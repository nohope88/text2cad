#!/usr/bin/env python3
"""Stop the cycle the moment DISCOVER produces a winner, and SAY SO on telegram.

The run would die at `brief` anyway (PHASE_TIMEOUT_MULT was set to 2.5 before
the real cost-per-turn was measured), so the discover panel is the only part
worth keeping. Kill the driver as soon as discover.md carries a FRESH winner —
the stale discover.md from the previous cycle is still on disk, hence the mtime
guard.

The telegram alert is the whole point of this script, so it retries and records
whether it actually landed. A silently-dropped send would leave the human
believing the cycle is still running.
"""
import re
import subprocess
import time
from pathlib import Path

HERE = Path("/root/text2cad")
DISC = HERE / "out" / "_discover" / "discover.md"
FLAG = HERE / "logs" / "qwen-stop-alert.txt"
START = time.time()

ENV = {}
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        ENV[k.strip()] = v.strip().strip('"')
TOK, CHAT = ENV.get("TELEGRAM_BOT_TOKEN", ""), ENV.get("TELEGRAM_CHAT_DM", "")


def tg(text):
    """Send, retry, record. Never assume it landed."""
    for attempt in (1, 2, 3):
        r = subprocess.run(["curl", "-s", "--max-time", "30",
                            f"https://api.telegram.org/bot{TOK}/sendMessage",
                            "-d", f"chat_id={CHAT}",
                            "--data-urlencode", f"text={text}"],
                           capture_output=True, text=True)
        if '"ok":true' in r.stdout:
            FLAG.write_text(f"SENT attempt {attempt}\n{text}\n", encoding="utf-8")
            print(f"[telegram ok attempt {attempt}]", flush=True)
            return True
        print(f"[telegram FAILED {attempt}] {r.stdout[:200]}", flush=True)
        time.sleep(5)
    FLAG.write_text(f"SEND FAILED 3x\n{text}\n", encoding="utf-8")
    return False


def pids(pat):
    r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()] if r.returncode == 0 else []


def vn():
    return subprocess.run(["date", "+%H:%M"], capture_output=True, text=True,
                          env={"TZ": "Asia/Ho_Chi_Minh", "PATH": "/usr/bin:/bin"}
                          ).stdout.strip()


if not (TOK and CHAT):
    print("NO TELEGRAM CREDS — refusing to arm", flush=True)
    raise SystemExit(1)
print(f"armed {vn()} VN — watching for a WINNER newer than launch", flush=True)

while True:
    time.sleep(10)
    driver = pids("python3 [.]/text2cad --discover")
    if not driver:
        tg(f"WARNING text2cad qwen ({vn()} VN): the driver exited on its own "
           f"BEFORE the discover stop fired. That is NOT the planned shutdown "
           f"— check logs/qwen-cycle-20260818.log")
        break
    if DISC.is_file() and DISC.stat().st_mtime > START:
        text = DISC.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^WINNER:\s*([a-z0-9-]+)", text, re.M)
        if m:
            # Silence the failure watchdog FIRST: this shutdown is deliberate,
            # and its "cycle ENDED" alarm here would be a false positive.
            for pat in ("cycle_alert[.]py", "claude -p IMPORTANT"):
                for p in pids(pat):
                    subprocess.run(["kill", str(p)], capture_output=True)
            for p in driver:
                subprocess.run(["kill", str(p)], capture_output=True)
            time.sleep(3)
            still = pids("python3 [.]/text2cad --discover")
            pm = re.search(r"^PROMPT:\s*(.+)$", text, re.M)
            tg(f"DONE text2cad qwen ({vn()} VN): DISCOVER finished, cycle "
               f"STOPPED as planned, before brief.\n\n"
               f"WINNER: {m.group(1)}\n\n"
               f"{('PROMPT: ' + pm.group(1)[:400]) if pm else ''}\n\n"
               f"driver killed: {'NO - STILL ALIVE' if still else 'yes'}\n"
               f"Next: relaunch from this winner with per-phase timeouts sized "
               f"on turns x 89s.")
            print("stopped at discover; winner:", m.group(1), flush=True)
            break
