#!/usr/bin/env python3
"""Event stream for the session's Monitor: one stdout line per thing worth waking for.

Watches THREE things, because silence must never be ambiguous:
  1. new authorised messages appended to tg/inbox.jsonl  -> the actual commands
  2. the daemon dying or coming back                     -> otherwise a crashed
     bridge is indistinguishable from "nobody messaged"
  3. relay/poll failures in bridge.log (409 Conflict, network) -> the bridge is
     up but deaf

Pure python polling, no shell pipes: `grep`/`awk` line-buffering is the classic
way these watchers silently swallow events.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

STATE = Path("/root/text2cad/tg")
INBOX = STATE / "inbox.jsonl"
LOG = STATE / "bridge.log"
FAIL = ("getUpdates failed", "Conflict", "poll loop error", "handle() failed")


def alive() -> bool:
    return subprocess.run(["pgrep", "-f", r"python3 .*[t]g_bridge\.py"],
                          capture_output=True).returncode == 0


def tail_pos(p: Path) -> int:
    return p.stat().st_size if p.is_file() else 0


def read_new(p: Path, pos: int):
    if not p.is_file():
        return [], pos
    size = p.stat().st_size
    if size < pos:            # truncated/rotated — start over
        pos = 0
    if size == pos:
        return [], pos
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(pos)
        data = fh.read()
        pos = fh.tell()
    return data.splitlines(), pos


def main() -> int:
    ipos, lpos = tail_pos(INBOX), tail_pos(LOG)
    was_alive = alive()
    last_check = time.time()
    if not was_alive:
        print("TG BRIDGE DOWN — tg-bridge.service is not running", flush=True)

    while True:
        lines, ipos = read_new(INBOX, ipos)
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                print(f"TG MSG (unparsed): {ln[:300]}", flush=True)
                continue
            print(f"TG MSG from {d.get('from')}: {str(d.get('text',''))[:800]}", flush=True)

        lines, lpos = read_new(LOG, lpos)
        for ln in lines:
            if any(f in ln for f in FAIL):
                print(f"TG BRIDGE ERROR: {ln.strip()[:300]}", flush=True)

        if time.time() - last_check > 60:
            last_check = time.time()
            now = alive()
            if now != was_alive:
                print("TG BRIDGE " + ("RECOVERED — polling again" if now else
                                       "DOWN — tg-bridge.service died"), flush=True)
                was_alive = now

        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
