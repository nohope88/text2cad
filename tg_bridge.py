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

import tg_router

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
    cmd = ["curl", "-s", "--max-time", str(timeout), "-K", "-"]
    for k, v in (params or {}).items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    r = subprocess.run(cmd, input='url = "' + f"{API}/{method}" + '"\n', capture_output=True, text=True)
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


MEDIA = STATE / "media"


VIDEO_FRAMES = 6          # frames on the contact sheet a session reads in place of the video


def video_kind(m: dict) -> str | None:
    """The key holding a video in this message, if any: a video, a round video note, a GIF
    animation, or a video sent as a file."""
    for k in ("video", "video_note", "animation"):
        if m.get(k):
            return k
    if str((m.get("document") or {}).get("mime_type", "")).startswith("video/"):
        return "document"
    return None


def frame_sheet(video: Path) -> Path | None:
    """A contact sheet of VIDEO_FRAMES evenly spaced frames, so a session (which reads images,
    not video) can see what the clip shows. None when ffmpeg cannot read the file."""
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(video)], capture_output=True, text=True,
                                   timeout=60).stdout.strip() or 0)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        dur = 0
    if dur <= 0:
        log(f"media: no duration for {video.name}")
        return None
    out = video.with_name(video.stem + "_frames.jpg")
    rate = VIDEO_FRAMES / dur
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
                        f"fps={rate:.6f},scale=480:-2,tile=3x2", "-frames:v", "1", "-q:v", "3", str(out)],
                       capture_output=True, text=True, timeout=180)
    if r.returncode or not out.exists():
        log(f"media: frame sheet failed for {video.name}: {r.stderr[-200:]}")
        return None
    return out


def save_media(m: dict) -> list[str]:
    """Download a message's photo (largest size), image document or video next to the inbox,
    so a session can open it; a video also gets a frame sheet. Best effort: a failure is
    logged and the text still goes through. Each entry is "<label>: <path>"."""
    items = []
    if m.get("photo"):
        items.append((m["photo"][-1]["file_id"], ".jpg", "image"))
    doc = m.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        ext = Path(doc.get("file_name") or "x.png").suffix or ".png"
        items.append((doc["file_id"], ext, "image"))
    vk = video_kind(m)
    if vk:
        v = m[vk]
        ext = Path(v.get("file_name") or "x.mp4").suffix or ".mp4"
        items.append((v["file_id"], ext, "video"))
    saved = []
    for i, (fid, ext, label) in enumerate(items):
        r = api("getFile", {"file_id": fid}, timeout=30)
        fpath = (r.get("result") or {}).get("file_path") if r.get("ok") else None
        if not fpath:
            # the cloud Bot API serves files up to 20 MB; a bigger video fails here
            log(f"media: getFile failed for message {m.get('message_id')}: {r.get('description')}")
            if label == "video":
                saved.append(f"video not downloaded: {r.get('description') or 'getFile failed'}"
                             " (the bot can fetch files up to 20 MB)")
            continue
        MEDIA.mkdir(parents=True, exist_ok=True)
        out = MEDIA / f"{m.get('message_id')}_{i}{ext}"
        dl = subprocess.run(["curl", "-s", "--max-time", "300", "-o", str(out),
                             "-K", "-"], input=('url = "' + f"{API.replace('/bot', '/file/bot', 1)}/{fpath}" + '"\n').encode(), capture_output=True)
        if dl.returncode == 0 and out.exists() and out.stat().st_size > 0:
            saved.append(f"{label} saved: {out}")
            if label == "video":
                sheet = frame_sheet(out)
                if sheet:
                    saved.append(f"video frames ({VIDEO_FRAMES}, left to right, top to bottom): {sheet}")
        else:
            log(f"media: download failed for message {m.get('message_id')} (curl rc={dl.returncode})")
    return saved


def body_of(m: dict) -> str:
    """Text, caption, or an explicit marker for media we cannot act on."""
    if m.get("text"):
        return m["text"]
    if m.get("caption"):
        return m["caption"]
    if "photo" in m or str((m.get("document") or {}).get("mime_type", "")).startswith("image/"):
        return "[image]"
    if video_kind(m):
        return "[video]"
    for kind in ("voice", "audio", "video", "document", "sticker"):
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

Many sessions on one bot:
/sessions        — who is listening, and which one gets your messages (→)
/use <name>      — send everything to that session from now on
/to <name> <msg> — one message to that session only, active unchanged
/all <msg>       — every session at once

With no session named, everything is broadcast — the old single-session
behaviour. A session names itself with the tg_session tool.

Anything else is treated as a plain instruction and acted on directly.

About /compact: it writes a durable handoff, it does NOT free the terminal
session's context window. Only typing /compact in the terminal can do that —
no external command can drive a running session. Use this before restarting
the session, or when you want a snapshot on record."""


# A Monitor expires every <=30 min and the session re-arms it when its current
# tool call ends; the re-armed watcher replays what arrived meanwhile. Warn only
# when no watcher has run for this long, i.e. the session is really gone.
WATCH_GRACE_S = 600


def watcher_alive() -> bool:
    """True when some Claude session runs tg_watch.py (the inbox Monitor), or ran one recently."""
    if subprocess.run(["pgrep", "-f", r"[t]g_watch\.py"], capture_output=True).returncode == 0:
        return True
    try:
        return time.time() - (STATE / "watch_seen").stat().st_mtime < WATCH_GRACE_S
    except OSError:
        return False


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
    # The singular is what the operator actually reached for first; a
    # near-miss must not fall through to a session as a plain instruction.
    if cmd in ("/sessions", "/session", "/ses", "/who"):
        return tg_router.describe()
    if cmd in ("/use", "/switch", "/talk"):
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            return "usage: /use <name>\n\n" + tg_router.describe()
        name = parts[1].strip().split()[0].lower()
        known = {r["name"] for r in tg_router.sessions()}
        if name not in known:
            return f"no session named {name!r}.\n\n" + tg_router.describe()
        tg_router.set_active(name)
        live = name in tg_router.live_names()
        warn = "" if live else ("\n\n⚠️ that session's process is GONE — messages will "
                                "queue for it until it comes back or you /use another.")
        return f"✅ active session is now {name} — your messages go there." + warn
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

    target, routed = tg_router.target_for(text)
    if target is None:
        # Names a session nobody registered. Delivering this to the active
        # session would act on an instruction aimed somewhere else, so it is
        # refused loudly instead.
        api("sendMessage", {"chat_id": chat, "reply_to_message_id": m.get("message_id"),
                            "text": "unknown session.\n\n" + tg_router.describe()}, timeout=20)
        log(f"MISROUTE from={who} text={text[:80]!r}")
        return
    if not routed.strip():
        api("sendMessage", {"chat_id": chat, "reply_to_message_id": m.get("message_id"),
                            "text": "usage: /to <name> <message>   (or /all <message>)"}, timeout=20)
        return

    media = save_media(m)
    if media:
        routed = routed + "\n" + "\n".join(f"[{item}]" for item in media)
    rec = {
        "update_id": update["update_id"],
        "target": target,
        "message_id": m.get("message_id"),
        "ts": m.get("date", int(time.time())),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("date", time.time()))),
        "chat_id": chat,
        "from": who,
        "text": routed,
        **({"media": [i.split(": ", 1)[1] for i in media if ": /" in i]} if media else {}),
    }
    with INBOX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"ACCEPT  from={who} target={target} text={rec['text'][:100]!r}")

    if target != tg_router.FOUNDRY and not watcher_alive():   # foundry reads the inbox on its own timer
        # Nobody will ever read this unless a session arms tg_watch.py — say so
        # NOW instead of leaving the operator staring at 👀 (2026-08-29 incident:
        # a session sent stage alerts for 90 min while its inbox sat unread).
        api("sendMessage", {"chat_id": chat, "reply_to_message_id": m.get("message_id"),
                            "text": ("⚠️ No Claude session is listening right now (tg_watch.py not "
                                     "armed). Your message is queued and will be read when a "
                                     "session arms the watcher — if it matters, ping the terminal.")},
            timeout=20)
        log(f"UNWATCHED from={who} — warned operator")

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
