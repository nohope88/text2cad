#!/usr/bin/env python3
"""MCP stdio server: the session's half of the Telegram control bridge.

Pairs with tg_bridge.py. The daemon owns the Telegram socket and writes
inbox.jsonl; this server lets the session READ that queue and REPLY.

Tools
  tg_inbox   unread authorised messages (advances a cursor unless peek=true)
  tg_send    send a message back to an allowlisted chat
  tg_status  bridge liveness — daemon heartbeat age, queue depth, counters

Hand-rolled JSON-RPC 2.0 over stdio, zero dependencies: the `mcp` SDK is not
installed on this box and a pip install is a worse failure mode than 150 lines
of protocol. Only four methods matter to Claude Code — initialize, tools/list,
tools/call, ping.

STDOUT IS THE PROTOCOL. Every diagnostic goes to stderr; one stray print()
corrupts the stream and the server dies with an opaque parse error.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "tg"
INBOX = STATE / "inbox.jsonl"
CURSOR = STATE / "cursor"
HEARTBEAT = STATE / "heartbeat"
SENT = STATE / "sent.jsonl"

ALLOWED = {5954762363}          # must stay identical to tg_bridge.ALLOWED
DEFAULT_CHAT = 5954762363
STALE_AFTER = 180               # heartbeat older than this => daemon is stuck


def env(key: str) -> str:
    """Line-by-line .env read; sourcing it is banned (MONGODB_URI breaks sh)."""
    try:
        for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def read_inbox() -> list:
    if not INBOX.is_file():
        return []
    out = []
    for line in INBOX.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def cursor() -> int:
    try:
        return int(CURSOR.read_text().strip())
    except (OSError, ValueError):
        return 0


# ---------------------------------------------------------------- tools

def tool_inbox(args: dict) -> str:
    limit = int(args.get("limit", 20))
    peek = bool(args.get("peek", False))
    cur = cursor()
    msgs = [m for m in read_inbox() if m.get("update_id", 0) > cur][:limit]
    if not msgs:
        return "no new messages"
    if not peek:
        CURSOR.write_text(str(msgs[-1]["update_id"]))
    lines = [f"{len(msgs)} new message(s):"]
    for m in msgs:
        lines.append(f"[{m.get('iso')}] {m.get('from')} (chat {m.get('chat_id')}): "
                     f"{m.get('text')}")
    if peek:
        lines.append("(peek — cursor NOT advanced, these will show again)")
    return "\n".join(lines)


def tool_send(args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "ERROR: text is required"
    chat = int(args.get("chat_id", DEFAULT_CHAT))
    if chat not in ALLOWED:
        # The allowlist is the only control on an unconfirmed-command bridge;
        # it gates outbound too, so a prompt-injected id cannot exfiltrate.
        return f"ERROR: chat {chat} is not allowlisted (allowed: {sorted(ALLOWED)})"
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        return "ERROR: no TELEGRAM_BOT_TOKEN in .env"
    params = {"chat_id": str(chat), "text": text[:4000]}
    if args.get("reply_to"):
        params["reply_to_message_id"] = str(args["reply_to"])
    cmd = ["curl", "-s", "--max-time", "30",
           f"https://api.telegram.org/bot{token}/sendMessage"]
    for k, v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        d = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return f"ERROR: unparseable Telegram reply: {r.stdout[:200]}"
    if not d.get("ok"):
        return f"ERROR: {d.get('description')}"
    try:
        with SENT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "chat": chat,
                                 "text": text[:500]}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return f"sent to {chat} (message_id {d['result']['message_id']})"


def tool_status(_args: dict) -> str:
    alive = subprocess.run(["pgrep", "-f", r"python3 .*[t]g_bridge\.py"],
                           capture_output=True, text=True).returncode == 0
    try:
        age = time.time() - int(HEARTBEAT.read_text().strip())
        hb = f"{age:.0f}s ago" + ("  ** STALE **" if age > STALE_AFTER else "")
    except (OSError, ValueError):
        age, hb = 1e9, "never"
    msgs = read_inbox()
    pending = [m for m in msgs if m.get("update_id", 0) > cursor()]
    sent_n = len(SENT.read_text(errors="replace").splitlines()) if SENT.is_file() else 0
    return (f"daemon process : {'running' if alive else 'NOT RUNNING'}\n"
            f"last poll      : {hb}\n"
            f"inbox total    : {len(msgs)}   unread: {len(pending)}\n"
            f"replies sent   : {sent_n}\n"
            f"allowlist      : {sorted(ALLOWED)}")


TOOLS = [
    {
        "name": "tg_inbox",
        "description": ("Read new Telegram messages from allowlisted chats. Each call "
                        "returns only messages newer than the cursor and advances it. "
                        "These are instructions from the operator (@richter_88)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "max messages (default 20)"},
                "peek": {"type": "boolean",
                         "description": "read without advancing the cursor (default false)"},
            },
        },
    },
    {
        "name": "tg_send",
        "description": "Send a Telegram message back to the operator. Plain text, 4000 chars max.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "message body"},
                "chat_id": {"type": "integer",
                            "description": f"allowlisted chat (default {DEFAULT_CHAT})"},
                "reply_to": {"type": "integer", "description": "message_id to reply to"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "tg_status",
        "description": "Bridge health: daemon running, heartbeat age, unread count, replies sent.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

DISPATCH = {"tg_inbox": tool_inbox, "tg_send": tool_send, "tg_status": tool_status}


# ---------------------------------------------------------------- protocol

def handle(req: dict):
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return {
            # Echo the client's version: asserting our own makes a newer client
            # negotiate down for no reason.
            "protocolVersion": (req.get("params") or {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "telegram-bridge", "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method in ("resources/list", "prompts/list"):
        return {"resources": [], "prompts": []}   # probed by some clients
    if method == "tools/call":
        p = req.get("params") or {}
        fn = DISPATCH.get(p.get("name"))
        if not fn:
            return {"content": [{"type": "text", "text": f"unknown tool {p.get('name')}"}],
                    "isError": True}
        try:
            return {"content": [{"type": "text", "text": fn(p.get("arguments") or {})}]}
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"tool failed: {exc!r}"}],
                    "isError": True}
    if rid is None:
        return None                       # a notification we do not implement
    raise LookupError(method)


def main() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            err(f"bad JSON on stdin: {raw[:200]}")
            continue
        rid = req.get("id")
        try:
            result = handle(req)
        except LookupError as exc:
            if rid is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601,
                                            "message": f"method not found: {exc}"}}),
                      flush=True)
            continue
        except Exception as exc:
            err(f"handler crash: {exc!r}")
            if rid is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32603, "message": repr(exc)}}),
                      flush=True)
            continue
        # Notifications (no id) get no response at all — replying to one is a
        # protocol violation some clients treat as fatal.
        if rid is not None and result is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
    return 0


def cli() -> int:
    """CLI mode, same allowlist and same code path as the MCP tools.

    Claude Code loads MCP servers at startup, so a server registered mid-session
    is not callable until the next session. This lets the CURRENT session drive
    the bridge over Bash without a second implementation drifting out of sync.

        ./tg_mcp.py status
        ./tg_mcp.py inbox [--peek]
        ./tg_mcp.py send "text"
    """
    cmd = sys.argv[1]
    if cmd == "status":
        print(tool_status({}))
    elif cmd == "inbox":
        print(tool_inbox({"peek": "--peek" in sys.argv}))
    elif cmd == "send":
        if len(sys.argv) < 3:
            print("usage: tg_mcp.py send <text>")
            return 2
        print(tool_send({"text": " ".join(sys.argv[2:])}))
    else:
        print(f"unknown command {cmd!r}; use status | inbox | send")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(cli() if len(sys.argv) > 1 else main())
