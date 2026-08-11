#!/usr/bin/env python3
"""Daily autonomous cycle: discover -> build -> output, no human gate.

Cron (panda, UTC):  15 0 * * *  cd /root/text2cad && ./autoloop.py >> logs/autoloop.log 2>&1

- Idempotent: one cycle per day (marker file), safe to re-run.
- Auto-commits ONLY the lessons tier (lessons.md, discover_lessons.md) — code
  changes belong to improve.py's PR flow.
- Heartbeat file for watchdog.sh (dead-man pattern: silence must be alarmable).
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
MARKERS = HERE / "out" / ".cycles"
LESSON_FILES = ["lessons.md", "discover_lessons.md"]


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, **kw)


def telegram(text):
    env = os.environ
    tok, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_DM", "")
    if tok and chat:
        sh(["curl", "-s", f"https://api.telegram.org/bot{tok}/sendMessage",
            "-d", f"chat_id={chat}", "--data-urlencode", f"text={text}"], timeout=30)


def load_env():
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main() -> int:
    load_env()
    LOGS.mkdir(exist_ok=True)
    MARKERS.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    (HERE / ".heartbeat").write_text(today + "\n", encoding="utf-8")
    marker = MARKERS / today
    if marker.exists():
        print(f"[{today}] cycle already ran — skip")
        return 0

    print(f"[{today}] cycle start")
    env = dict(os.environ)
    env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "")
    r = subprocess.run([str(HERE / "text2cad"), "--discover", "--auto"],
                       cwd=HERE, env=env, capture_output=True, text=True,
                       timeout=3 * 3600)
    log = LOGS / f"cycle-{today}.log"
    log.write_text(r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8")
    marker.write_text("", encoding="utf-8")

    # summarize from the tail of the run
    tail = "\n".join(r.stdout.strip().splitlines()[-6:])
    slug = cost = gate = "?"
    for ln in r.stdout.splitlines():
        if ln.startswith("== DONE "):
            slug = ln.split()[2].rstrip(":")
            gate = "PASS" if "gate=PASS" in ln else "FAIL"
        if "total LLM cost" in ln:
            cost = ln.split("$")[-1].split()[0]
    status = "OK" if r.returncode == 0 and gate == "PASS" else "NEEDS ATTENTION"
    telegram(f"text2cad daily cycle [{status}]\n{tail[:2500]}")

    # lessons tier auto-commit (code tier is improve.py's PR flow)
    changed = [f for f in LESSON_FILES
               if sh(["git", "diff", "--quiet", "--", f]).returncode != 0]
    if changed:
        sh(["git", "add", *changed])
        sh(["git", "commit", "-m", f"loop: lessons update {today} ({slug}, ${cost})"])
        p = sh(["git", "push"])
        print("lessons committed+pushed" if p.returncode == 0 else f"push failed: {p.stderr[-200:]}")

    print(f"[{today}] cycle done: {slug} gate={gate} cost=${cost}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
