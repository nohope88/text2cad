#!/usr/bin/env python3
"""Telegram -> this box: long-poll daemon for @generate_news_video_bot.

The inbound half of the Telegram control bridge. MCP tools are PULL-only — an
MCP server cannot push a message into a running Claude Code session — so this
daemon owns the socket, writes every authorised message to inbox.jsonl, and the
session watches that file (Monitor) and answers through tg_mcp.py's tg_send.

    ./tg_bridge.py                 # foreground, long-poll forever
    ./tg_bridge.py --once          # single poll, for smoke tests

SECURITY: the session this feeds runs with bypassPermissions over the whole
box, so an accepted message is an unconfirmed command here. ALLOWED is the only
control left. Anything from another chat is dropped before it is ever written to
inbox.jsonl, and logged as REJECT so an attempt is visible.

Exactly ONE getUpdates consumer may exist per bot token. Telegram answers a
second one with 409 Conflict; if that shows up in the log, another poller (or a
stale copy of this daemon) is running.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "tg"
INBOX = STATE / "inbox.jsonl"
OFFSET = STATE / "offset"
HEARTBEAT = STATE / "heartbeat"
LOG = STATE / "bridge.log"

# Only these chat ids may drive the session. 5954762363 == @richter_88, the same
# DM cycle_alert.py already alerts to. Resolved via getChat, not guessed.
ALLOWED = {5954762363}

POLL_TIMEOUT = 50          # Telegram long-poll seconds; 50 is the practical max
NET_BACKOFF = 5.0


def env(key: str) -> str:
    """Read one key out of .env WITHOUT sourcing it.

    `set -a; . .env` is banned on this box — MONGODB_URI's value breaks shell
    parsing and takes every other export down with it.
    """
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return ""


TOKEN = env("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}"


# systemd sets INVOCATION_ID and already appends our stdout to bridge.log —
# writing the file ourselves too duplicates every line.
UNDER_SYSTEMD = bool(os.environ.get("INVOCATION_ID"))


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if UNDER_SYSTEMD:
        return
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def api(method: str, params=None, timeout=70):
    """curl, not urllib — same reason the media gateway needs it: python-urllib
    user agents get 1010'd by Cloudflare on this network path."""
    cmd = ["curl", "-s", "--max-time", str(timeout), f"{API}/{method}"]
    for k, v in (params or {}).items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not r.stdout.strip():
        return {"ok": False, "description": f"empty reply (curl rc={r.returncode})"}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "description": r.stdout[:300]}


def read_offset() -> int:
    try:
        return int(OFFSET.read_text().strip())
    except (OSError, ValueError):
        return 0


def write_offset(n: int) -> None:
    tmp = OFFSET.with_suffix(".tmp")
    tmp.write_text(str(n))
    tmp.replace(OFFSET)          # atomic: a crash mid-write must not replay


def body_of(m: dict) -> str:
    """Text, caption, or an explicit marker for media we cannot act on."""
    if m.get("text"):
        return m["text"]
    if m.get("caption"):
        return m["caption"]
    for kind in ("photo", "voice", "audio", "video", "document", "sticker"):
        if kind in m:
            return f"[{kind} — not supported by the bridge, send text]"
    return "[empty message]"


HELP = """Commands

/ping     — is the bridge alive (answered by the daemon, works even if the
            Claude session is down)
/help     — this text
/status   — what is running: pipeline, proxy, bridge
/compact  — snapshot the session to tg/handoff.md and send a digest
/stop     — kill the pipeline driver and watchdogs
/log N    — last N lines of the current cycle log

Anything else is treated as a plain instruction and acted on directly.

About /compact: it writes a durable handoff, it does NOT free the terminal
session's context window. Only typing /compact in the terminal can do that —
no external command can drive a running session. Use this before restarting
the session, or when you want a snapshot on record."""


def fast_reply(text: str):
    """Commands the DAEMON answers itself, without waking the session.

    Deliberately only the two that need no judgement. Everything else must
    reach the session, because answering it here would mean maintaining a
    second, always-staler idea of what the box is doing.
    """
    cmd = text.strip().split()[0].lower().split("@")[0] if text.strip() else ""
    if cmd == "/ping":
        return ("pong — daemon up. (This proves the BRIDGE is alive. If the "
                "Claude session is down or its Monitor is not armed you will "
                "get 👀 on messages and no answer.)")
    if cmd == "/help":
        return HELP
    return None


def handle(update: dict) -> None:
    m = update.get("message") or update.get("edited_message")
    if not m:
        return
    chat = (m.get("chat") or {}).get("id")
    frm = m.get("from") or {}
    who = "@" + (frm.get("username") or str(frm.get("id")))

    if chat not in ALLOWED:
        # Dropped before it can reach the session. Logged so an attempt is
        # never silent, but NOT written to inbox.jsonl and never answered —
        # replying would confirm the bot is live to whoever is probing.
        log(f"REJECT chat={chat} from={who} text={body_of(m)[:80]!r}")
        return

    text = body_of(m)
    quick = fast_reply(text)
    if quick is not None:
        # Answered here; the session is never woken for it.
        api("sendMessage", {"chat_id": chat, "text": quick,
                            "reply_to_message_id": m.get("message_id")}, timeout=20)
        log(f"FAST    from={who} cmd={text.split()[0]!r}")
        return

    rec = {
        "update_id": update["update_id"],
        "message_id": m.get("message_id"),
        "ts": m.get("date", int(time.time())),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("date", time.time()))),
        "chat_id": chat,
        "from": who,
        "text": text,
    }
    with INBOX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"ACCEPT  from={who} text={rec['text'][:100]!r}")

    # 👀 = "the box has it". Not "answered" — the answer is the session's reply.
    # If this reaction never appears, the daemon is down; if it appears and no
    # reply follows, the daemon is up and the SESSION is down. Cheap triage.
    api("setMessageReaction", {
        "chat_id": chat, "message_id": m.get("message_id"),
        "reaction": json.dumps([{"type": "emoji", "emoji": "👀"}]),
    }, timeout=15)


def poll_once(offset: int) -> int:
    r = api("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT,
                           "allowed_updates": json.dumps(["message", "edited_message"])},
            timeout=POLL_TIMEOUT + 20)
    HEARTBEAT.write_text(str(int(time.time())))
    if not r.get("ok"):
        desc = str(r.get("description"))
        log(f"getUpdates failed: {desc}")
        if "Conflict" in desc:
            log("  -> another getUpdates consumer holds this token. "
                "Stop the other poller; two cannot share a bot.")
        time.sleep(NET_BACKOFF)
        return offset
    for u in r.get("result", []):
        try:
            handle(u)
        except Exception as exc:                       # one bad update must not
            log(f"handle() failed on {u.get('update_id')}: {exc!r}")  # kill the loop
        offset = u["update_id"] + 1
        write_offset(offset)
    return offset


def main() -> int:
    if not TOKEN:
        log("no TELEGRAM_BOT_TOKEN in .env — refusing to start")
        return 2
    STATE.mkdir(exist_ok=True)
    me = api("getMe", timeout=20)
    if not me.get("ok"):
        log(f"getMe failed: {me.get('description')}")
        return 2
    log(f"bridge up as @{me['result']['username']} — allowlist {sorted(ALLOWED)}")
    # Makes the commands tappable from the Telegram compose bar.
    api("setMyCommands", {"commands": json.dumps([
        {"command": "ping", "description": "is the bridge alive"},
        {"command": "help", "description": "list commands"},
        {"command": "status", "description": "what is running on the box"},
        {"command": "compact", "description": "snapshot session to handoff.md"},
        {"command": "stop", "description": "kill pipeline driver + watchdogs"},
        {"command": "log", "description": "tail the cycle log"},
    ])}, timeout=20)

    offset = read_offset()
    if offset == 0:
        # First start: skip whatever is already queued rather than acting on a
        # backlog of messages sent before the bridge existed.
        r = api("getUpdates", {"offset": -1, "timeout": 1}, timeout=25)
        if r.get("ok") and r.get("result"):
            offset = r["result"][-1]["update_id"] + 1
            write_offset(offset)
            log(f"skipped {len(r['result'])} pre-existing update(s), offset={offset}")

    once = "--once" in sys.argv
    while True:
        try:
            offset = poll_once(offset)
        except KeyboardInterrupt:
            log("interrupted")
            return 0
        except Exception as exc:
            log(f"poll loop error: {exc!r}")
            time.sleep(NET_BACKOFF)
        if once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
