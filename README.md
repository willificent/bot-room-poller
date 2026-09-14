# Bot Room Poller

Pull-based turn-taking for AI agents sharing a Mattermost room — no webhooks, no
`require_mention` disabling, no parroting. Two (or more) bots poll a room on
staggered cron ticks, a cheap LLM gatekeeper decides whether the latest message
warrants a response, and only then does the full agent compose a reply.

Born from a real deployment: two sibling agents sharing a group chat with their
humans, running live since September 2026. This repo is the sanitized, portable
version.

## Why pull instead of push?

Mattermost gives you two ways to make an agent react in a channel, and both
fail for natural multi-party conversation:

| Approach | Problem |
|---|---|
| **Webhook-triggered turns** | Every message fires a turn. Two reactive agents ping-pong forever — nobody can *not* respond. |
| **`require_mention: false`** | Same trap, plus the gateway now owns every message, so the agent replies even when the conversation was clearly between others. |

The missing primitive is **deciding not to reply**. Humans do it constantly:
someone reads the room and stays quiet when the conversation has closed or when
the others have it handled. This project implements exactly that decision as an
explicit, logged, LLM-gated judgment call.

## Architecture

```
                 Mattermost room (ordinary channel, requires @mention for gateways)
                 ┌────────────────────────────────────────────┐
                 │  Human-1, Human-2, Bot-1, Bot-2            │
                 └────────────────────────────────────────────┘
                        ▲ posts via REST        │ reads via REST
                        │                       ▼
   ┌────────────────────┴────────┐     ┌──────────────────────────┐
   │ poller Bot-1                │     │ poller Bot-2             │
   │ cron */2 * * * *            │     │ cron 1-59/2 * * * *      │
   │  (even minutes)             │     │  (odd minutes)           │
   └──────────┬──────────────────┘     └──────────┬───────────────┘
              │                                   │
              ▼                                   ▼
        [Gate 1: free] latest msg newer than last seen? ball in my court?
              │ yes                              │ yes
              ▼                                   ▼
        [Gate 2: cheap LLM judge] should I respond? (small model, e.g. gemma-class)
              │ yes                              │ yes
              ▼                                   ▼
        [Stage 3: full agent one-shot turn] → post reply → release lock
```

### The gates

**Gate 1 — deterministic, free.** No new message since last seen, own message
latest, cooldown window, or explicit @mention (gateway's job) → exit before any
LLM call. Most polls die here at zero cost.

**Gate 2 — the judgment call.** One call to a *cheap* LLM (not the main agent
model — in production this is a small gemma-class model via an OpenAI-compatible
endpoint) with the last 10 messages and a rubric. Returns
`{"respond": bool, "reason": "..."}`. The rubric explicitly scores:
questions/requests → yes; social closes, bare acknowledgments, messages addressed
to someone else, and 4+-message bot stalemates → no.

**Stage 3 — the reply.** Only on YES. The full agent runs a one-shot turn
(persistent sibling session, resumed by name, output = final text only) and the
result is posted via REST.

### Anti-parrot machinery

- **In-flight lock** — a shared file with TTL; while one bot is composing,
  the other's polls skip. No double-response to the same message.
- **Close detection** — "great chat / see you later / sounds good" from the
  other party + nothing substantive pending → conversation marked `closed`;
  closed stays closed until a substantive new message or a human speaks.
- **Cooldown** — hard minimum gap between a bot's own replies.
- **Stalemate detector** — 4+ consecutive bot messages with no question or
  request anywhere → no reply regardless of judge output.
- **Mention hand-off** — if the latest message @mentions a bot, its
  *gateway* handles it (native mention machinery); the poller stands down.
- **System-post filter** — Mattermost system messages (header changes, joins)
  are filtered out entirely; they are not conversation and must not consume
  a turn decision.

### Why the staggering works

Bot-1 polls even minutes, Bot-2 polls odd minutes. Because Gate 1 is free,
poll frequency and think speed are decoupled: a poll that finds "no new message"
costs two curl-equivalent calls, so *the poll is what makes waiting real*. The
sibling's finished reply is simply there whenever the next poll looks. Reply
latency in practice: poll wait (≤2 min) + turn time (30 s–3 min) + their poll
wait (≤2 min).

### Session lifecycle

Each bot keeps one persistent conversation session (resumed by name for
context continuity). When the room has been quiet for ≥10 minutes at the moment
a *new* turn arrives, the poller starts a fresh session — long silences mean
new conversations, and memory systems (each bot's persistent memory bank)
make resuming earlier threads possible whenever it matters.

## Setup

1. Create an ordinary Mattermost room; add your humans and both bot accounts.
2. Note the **channel ID** (visible in the channel page URL) and each member's
   **user ID** (`/api/v4/users?per_page=200` with any valid token).
3. Install dependencies: none beyond Python 3 stdlib. (The pollers use only
   `urllib`, `json`, `subprocess`.)
4. Copy `bot1.json.example` → your bot configs; fill in user IDs, token file
   paths (each bot's own Mattermost token), and the judge key file
   (OpenAI-compatible key used only for Gate 2's cheap judge calls).
5. Edit `room_poller.py` constants at the top: `CHANNEL_ID`, `MM_URL`,
   `OLLAMA_URL` (any OpenAI-compatible chat endpoint), `JUDGE_MODEL`, and the
   `HUMANS`/`BOTS`/`AGENT_NAMES` maps. If your Mattermost sits behind Cloudflare
   bot-fight mode, keep a browser-form `User-Agent` on every request — Python's
   default UA gets error 1010.
6. Cron wrappers: copy the `*.sh.example` files, adjust paths, wire them into
   your scheduler (these were built for Hermes `cron create --no-agent
   --script` with `local` delivery — silent no-ops clean their own output
   wrapper files).
7. **Start in dry-run** (`"dry_run": true` in both configs): pollers decide
   and log but never post. Read a day of logs. Then flip to `false`.

## Decision log doctrine

Every invocation logs exactly one line — a skip occupies the same slot as a
response, so a missing check is never indistinguishable from a passed one:

```
2026-09-14 12:30:13  Bot-1  SKIPPED      own message is latest
2026-09-14 12:30:14  Bot-2  WOULD-POST   judge=yes (direct question about the room name)  | DRY-RUN: stage3 not executed
2026-09-14 12:46:47  Bot-2  RESPOND      to Bot-1: Direct question asking for aspirations.  | <first 80 chars of reply>
```

## Battle scars (production lessons baked into this code)

1. **Allowlists match platform IDs, not usernames.** An allowlist also *shadows*
   the `allow_all_users` fallback — a message from an authorized-by-username
   human is silently dropped, no turn, nothing in the logs. Dropped messages
   produce *zero* turn activity; that's the diagnostic signature.
2. **Cloudflare bot-fight mode rejects Python's default User-Agent** (error
   1010) while curl sails through. Send a browser-form UA on every request.
3. **Back-port fixes you verify manually.** A CLI-flag fix proven by hand but
   not written into the calling code produced a live ERROR on the first cron
   tick. The fix existed; the code didn't have it.
4. **System messages bury the latest real message.** A channel-header edit
   became "latest post," both pollers consumed it as seen, and a genuine
   question underneath went unjudged. Filter `type`-bearing system posts
   *before* "latest" is computed.

## Files

| File | Purpose |
|---|---|
| `room_poller.py` | The poller — shared script, per-bot config via argv |
| `bot1.json.example` | Bot-1 config (dry-run default) |
| `bot2.json.example` | Bot-2 config (dry-run default) |
| `cron-wrapper-bot1.sh.example` | Scheduler silent wrapper, Bot-1 |
| `cron-wrapper-bot2.sh.example` | Scheduler silent wrapper, Bot-2 |

## License

MIT