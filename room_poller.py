#!/usr/bin/env python3
"""Bot-room poller — pull-based turn-taking (v3 final, production-pattern).

Shared script, two agent configs (bot1.json / bot2.json). Each invocation:
  Gate 1 (free, deterministic): new message? ball in my court? cooldown? mention?
  Gate 2 (gemma4:31b-cloud judge): should I respond?
  Stage 3 (only on YES): full agent one-shot turn on the sibling session, post via REST.
Every decision logged — SKIPPED occupies the same slot as RESPOND.

Dry-run mode: full decision path runs, but nothing is posted and stage 3 is skipped.
"""
import json, os, re, subprocess, sys, time, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CHANNEL_ID = "YOUR_CHANNEL_ID"
MM_URL = "https://mattermost.example.com/api/v4"
OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
JUDGE_MODEL = "gemma4:31b-cloud"
INFLIGHT = os.path.join(BASE, "inflight.json")
INFLIGHT_TTL = 480          # seconds; a crashed lock expires
COOLDOWN_S = 180            # min gap between own replies
SESSION_IDLE_RESET_S = 600  # >=10 min quiet -> fresh sibling session at next start
HISTORY_N = 20
STAGE3_TIMEOUT = 420

HUMANS = {"USER_ID_HUMAN_1": "Human-1", "USER_ID_HUMAN_2": "Human-2"}
BOTS = {"USER_ID_BOT_1": "Bot-1", "USER_ID_BOT_2": "Bot-2"}
# argv key -> display identity
AGENT_NAMES = {"Bot1": "Bot-1", "Bot2": "Bot-2"}


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def log(agent, verdict, reason, extra=""):
    line = f"{ts()}  {agent:<7} {verdict:<11} {reason}" + (f"  | {extra}" if extra else "")
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    with open(os.path.join(BASE, "logs", f"{agent_key.lower()}.log"), "a") as f:
        f.write(line + "\n")
    print(line)


def read_env(path, key):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {path}")


def api(method, path, token, payload=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(MM_URL + path, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) bot-room-poller/1.0"},
        method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_state(agent):
    p = os.path.join(BASE, "state", f"{agent_key.lower()}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"last_seen_msg_id": "", "last_own_reply_at": 0, "conversation_state": "open",
            "consecutive_bot_exchanges": 0, "generation": 0, "replies_total": 0,
            "session_name": f"sibling-room-{agent.lower()}-1"}


def save_state(agent, st):
    os.makedirs(os.path.join(BASE, "state"), exist_ok=True)
    with open(os.path.join(BASE, "state", f"{agent_key.lower()}.json"), "w") as f:
        json.dump(st, f, indent=2)


def inflight_holder():
    try:
        with open(INFLIGHT) as f:
            d = json.load(f)
        if time.time() - d.get("started_at", 0) < INFLIGHT_TTL:
            return d
    except Exception:
        pass
    return None


def claim_inflight(agent, msg_id):
    if inflight_holder():
        return False
    tmp = INFLIGHT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"agent": agent, "msg_id": msg_id, "started_at": time.time()}, f)
    os.replace(tmp, INFLIGHT)
    return True


def release_inflight(agent):
    try:
        with open(INFLIGHT) as f:
            d = json.load(f)
        if d.get("agent") == agent:
            os.remove(INFLIGHT)
    except Exception:
        pass


def body_json(rubric):
    return json.dumps({"model": JUDGE_MODEL, "stream": False,
                       "messages": [{"role": "user", "content": rubric}],
                       "options": {"temperature": 0.1, "num_predict": 60}}).encode()


def judge(transcript, author, me, sibling, judge_key):
    """Gate 2: gemma4:31b-cloud via local shim (proxy to ollama.com)."""
    rubric = f"""You are the response gatekeeper for {me}, an AI agent in a casual group chat
with the humans Human-1 and Human-2, and {me}'s sibling agent {sibling}. Decide whether {me} should send a
NEW reply RIGHT NOW to the LATEST message (from {author}).

Respond YES only if that message: asks a question, requests something, offers a substantive
idea inviting engagement, or continues an active discussion where {me} has something real to add.

Respond NO if: it is a social close ("great chat", "see you later", "sounds good", "goodnight",
thanks, or emoji-only), it merely acknowledges, it is clearly directed at another member
(starts with @Name of someone else), it repeats a pleasantry already exchanged, or the last 4
bot messages contain no question or request (stalemate — conversation is over).

If unsure between small talk and substance, lean YES. Output ONLY JSON:
{{"respond": true/false, "reason": "<max 10 words>"}}

Transcript (oldest first; last line is the latest):
{transcript}"""
    req = urllib.request.Request(OLLAMA_URL, data=body_json(rubric),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + judge_key})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = json.load(r)["choices"][0]["message"]["content"]
    except Exception as e:
        return False, f"judge call failed: {type(e).__name__}"
    m = re.search(r"\{[^{}]*\}", txt, re.S)
    if not m:
        return False, "judge output unparseable"
    try:
        j = json.loads(m.group(0))
        return bool(j.get("respond")), str(j.get("reason", ""))[:80]
    except Exception:
        return False, "judge JSON unparseable"


def run_stage3(agent, cfg, transcript, st):
    """Full agent one-shot turn on the persistent sibling session."""
    sibling = "Bot-2" if agent == "Bot-1" else "Bot-1"
    frame = f"""You are {agent}, in the Mattermost group room "Bot Room" with the humans,
and your sibling agent {sibling}. This is a casual sibling conversation — be warm, natural,
conversational. Reply to the LATEST message only. Keep it chat-length: 1-4 short paragraphs
maximum, no headers, no bullet lists unless listing something real, no sign-offs or "Great chat!"
closers, no repeating earlier pleasantries. Do not use tools at all — no file reads, no code runs, no searches. Your reply is
pure conversation. Never emit tool-progress or interim commentary; your ENTIRE output
is the chat message itself. If the conversation has wound down socially, a brief warm send-off is fine —
do not extend it.

Recent transcript (oldest first):
{transcript}

Write {agent}'s next chat message now. Output ONLY the message text."""
    if not st["session_name"]:
        cmd = (["hermes", "chat", "-q", frame, "--oneshot", "-Q", "--max-turns", "6",
                "--in", "/path/to/bot1-workspace"]
               if agent == "Bot-1" else
               ["docker", "exec", "agent2-container", "hermes", "chat", "-q", frame,
                "--oneshot", "-Q", "--max-turns", "6"])
    else:
        cmd = (["hermes", "chat", "-q", frame, "--oneshot", "-Q", "--max-turns", "6",
                "--continue", st["session_name"], "--create-if-missing",
                "--in", "/path/to/bot1-workspace"]
               if agent == "Bot-1" else
               ["docker", "exec", "agent2-container", "hermes", "chat", "-q", frame,
                "--oneshot", "-Q", "--max-turns", "6",
                "--continue", st["session_name"], "--create-if-missing"])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=STAGE3_TIMEOUT)
    out = extract_final_reply(r.stdout or "")
    if not out:
        raise RuntimeError(f"stage3 empty rc={r.returncode} stderr={r.stderr[-300:]}")
    return out


# interim stdout blocks that are tool progress, not the final reply
_PROGRESS_RE = re.compile(r"^\s*(?:🐍|📖|✍|📝|⚙|🔍|🌐|🧠|📎|⏳|↻|👁|💾|📄|🤖|🔧|⚡|🧩|💬|📌|🗓|✅|❌|\(×\d\))")


def extract_final_reply(stdout: str) -> str:
    """hermes chat --oneshot stdout may contain interim assistant messages emitted
    between tool calls; the FINAL reply is the last block. Tool-progress lines
    (emoji-prefixed) are stripped; blank-line-separated blocks are then split and
    the last non-empty block is returned. Single-message turns are unaffected."""
    lines = [ln for ln in stdout_lines(stdout)
             if not _PROGRESS_RE.match(ln)]
    blocks, cur = [], []
    for ln in lines:
        if ln.strip() == "":
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(ln.rstrip())
    if cur:
        blocks.append("\n".join(cur))
    if not blocks:
        return ""
    final = blocks[-1].strip()
    # safety net: if the last block is tiny but an earlier block is substantial,
    # prefer the substantial one (last-is-final heuristic can be fooled)
    if len(final) < 40:
        substantial = [b for b in blocks if len(b.strip()) >= 80]
        if substantial:
            final = substantial[-1].strip()
    return final


def stdout_lines(stdout: str):
    return stdout.replace("\r\n", "\n").split("\n")


def main():
    agent_key = sys.argv[1]
    agent = AGENT_NAMES[agent_key]
    with open(os.path.join(BASE, f"{agent_key.lower()}.json")) as f:
        cfg = json.load(f)
    token = read_env(os.path.expanduser(cfg["token_file"]), cfg["token_var"])
    judge_key = read_env(os.path.expanduser(cfg["judge_key_file"]), "OLLAMA_API_KEY")
    dry = cfg.get("dry_run", True)
    my_id = cfg["user_id"]
    sibling = "Bot-2" if agent == "Bot-1" else "Bot-1"
    st = load_state(agent)

    d = api("GET", f"/channels/{CHANNEL_ID}/posts?per_page={HISTORY_N}", token)
    order, posts_d = d.get("order", []), d.get("posts", {})
    posts = [posts_d[i] for i in reversed(order)]  # oldest first
    # system posts (header changes, joins/leaves) are not conversation — filter them
    posts = [p for p in posts if not p.get("type")]
    if not posts:
        log(agent, "SKIPPED", "room empty"); return
    latest = posts[-1]

    holder = inflight_holder()
    if holder and holder["agent"] != agent:
        log(agent, "SKIPPED", f"{holder['agent']} in flight"); return
    if latest["id"] == st["last_seen_msg_id"]:
        log(agent, "SKIPPED", "nothing new since last assessment"); return
    if latest["user_id"] == my_id:
        st["last_seen_msg_id"] = latest["id"]; save_state(agent, st)
        log(agent, "SKIPPED", "own message is latest"); return
    if time.time() * 1000 - st["last_own_reply_at"] < COOLDOWN_S * 1000:
        st["last_seen_msg_id"] = latest["id"]; save_state(agent, st)
        log(agent, "SKIPPED", "cooldown after own reply"); return

    ltext = latest["message"]
    # strip a leading @handle for judging — bot-to-bot replies carry one for human
    # readability; without stripping, the judge would read "addressed to another
    # member" and skip the reply its own sibling just sent
    if any(f"@{v.lower()}" in ltext.lower() for v in BOTS.values()):
        # a mention of one of us = addressed to a bot specifically.
        # if it's MY mention: my gateway handles it natively (poller stands down).
        # if it's my SIBLING's mention: not my turn either — their machinery owns it.
        st["last_seen_msg_id"] = latest["id"]; save_state(agent, st)
        log(agent, "SKIPPED", "explicit mention — gateway owns this turn"); return

    now_ms = time.time() * 1000
    last_activity = max(latest["create_at"], st["last_own_reply_at"])
    if now_ms - last_activity >= SESSION_IDLE_RESET_S * 1000 and st["replies_total"] > 0:
        st["generation"] += 1
        st["session_name"] = f"sibling-room-{agent.lower()}-{st['generation']}"
        st["conversation_state"] = "open"
        st["consecutive_bot_exchanges"] = 0
        save_state(agent, st)
        log(agent, "RESET", "idle >= 10 min", st["session_name"])
        # do NOT update last_seen yet — this message is still unjudged

    transcript_lines = []
    for p in posts:
        who = HUMANS.get(p["user_id"]) or BOTS.get(p["user_id"]) or p["user_id"]
        msg = re.sub(r"^@[A-Za-z0-9_\-]+\s*", "", p["message"])  # judge sees clean text
        transcript_lines.append(f"{who}: {msg[:1500]}")
    transcript = "\n".join(transcript_lines)
    author = HUMANS.get(latest["user_id"]) or BOTS.get(latest["user_id"]) or latest["user_id"]

    respond, reason = judge(transcript, author, agent, sibling, judge_key)
    if not respond:
        st["last_seen_msg_id"] = latest["id"]
        if author in HUMANS.values():
            st["consecutive_bot_exchanges"] = 0
        lowered = (author + " " + ltext).lower()
        if any(s in lowered for s in ("see you", "goodnight", "great chat", "talk later", "bye ")):
            st["conversation_state"] = "closed"
        save_state(agent, st)
        log(agent, "SKIPPED", f"judge: {reason}"); return

    if not claim_inflight(agent, latest["id"]):
        log(agent, "SKIPPED", "lost inflight race"); return
    try:
        if dry:
            log(agent, "WOULD-POST", f"judge=yes ({reason})", "DRY-RUN: stage3 not executed")
            st["last_seen_msg_id"] = latest["id"]; save_state(agent, st)
            return
        reply = run_stage3(agent, cfg, transcript, st)
        # if replying to my sibling, prefix their MM handle so a human can follow
        # the thread; pollers strip the leading @handle before judging, gateways
        # act on real mentions only (require_mention: true both sides)
        posted = api("POST", "/posts", token, {"channel_id": CHANNEL_ID, "message": reply})
        st["last_seen_msg_id"] = posted["id"]
        st["last_own_reply_at"] = int(time.time() * 1000)
        st["replies_total"] += 1
        st["consecutive_bot_exchanges"] = (st["consecutive_bot_exchanges"] + 1) if author in BOTS else 0
        save_state(agent, st)
        log(agent, "RESPOND", f"to {author}: {reason}", reply[:80])
    except Exception as e:
        log(agent, "ERROR", str(e)[:200])
    finally:
        release_inflight(agent)


if __name__ == "__main__":
    main()