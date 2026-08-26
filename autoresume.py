#!/usr/bin/env python3
"""Keep one product run alive: when it aborts for a KNOWN reason, relaunch it
with the budget that reason calls for, and say so on Telegram.

    ./autoresume.py <slug> <logfile> [--max-restarts 2]

Not a blind retry loop. It only acts on aborts whose cause is legible in the
log and whose fix is a bigger budget:

  ABORT: brief.md was not produced -> brief starved      -> double BRIEF_TURNS
  ABORT: no .stl produced          -> build starved      -> double BUILD_TURNS

Anything else — a clean finish, a quota death, an unrecognised abort — stops the
watcher and hands it to the human, because relaunching into an unknown failure
just burns the night twice.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path("/root/text2cad")
ENV = {}
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        ENV[k.strip()] = v.strip().strip('"')

BASE = {
    "TASTE_FILE": "taste_boardgame.md", "PARTS": "8-16", "NOVELTY": "8",
    "MECHANISM": "8", "ORNAMENT": "4", "CRAFT": "7",
    "SELFHOST_URL": "http://127.0.0.1:8099", "SELFHOST_KEY": "dummy",
    "SELFHOST_MODEL": "qwen/qwen3.8-27b", "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
    "BRIEF_TURNS": "60", "DRAFT_TURNS": "180", "BUILD_TURNS": "250",
    "REPAIR_TURNS": "180", "PHASE_TIMEOUT_MULT": "4",
}
CAUSES = [
    (re.compile(r"ABORT: brief\.md was not produced"), "BRIEF_TURNS", "brief starved"),
    (re.compile(r"ABORT: no \.stl produced"), "BUILD_TURNS", "build produced no geometry"),
]


def tg(text):
    for _ in range(3):
        r = subprocess.run(["curl", "-s", "--max-time", "30",
                            f"https://api.telegram.org/bot{ENV.get('TELEGRAM_BOT_TOKEN','')}/sendMessage",
                            "-d", f"chat_id={ENV.get('TELEGRAM_CHAT_DM','')}",
                            "--data-urlencode", f"text={text}"], capture_output=True, text=True)
        if '"ok":true' in r.stdout:
            print(f"[tg ok] {text[:90]}", flush=True)
            return
        time.sleep(5)
    print(f"[tg FAILED] {text[:90]}", flush=True)


def vn():
    return subprocess.run(["date", "+%H:%M"], capture_output=True, text=True,
                          env={"TZ": "Asia/Ho_Chi_Minh", "PATH": "/usr/bin:/bin"}).stdout.strip()


def alive(slug):
    r = subprocess.run(["pgrep", "-f", f"text2cad .*--slug {slug}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def main():
    slug, logname = sys.argv[1], sys.argv[2]
    a = sys.argv[3:]
    left = int(a[a.index("--max-restarts") + 1]) if "--max-restarts" in a else 2
    log = HERE / logname
    env = dict(BASE)
    prompt = Path("/tmp/winner_prompt.txt").read_text(encoding="utf-8").strip()
    print(f"autoresume armed for {slug}, {left} restart(s) in hand", flush=True)

    while True:
        time.sleep(30)
        if alive(slug):
            continue
        time.sleep(5)
        txt = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        cause = next(((var, why) for rx, var, why in CAUSES if rx.search(txt)), None)
        if cause is None:
            tg(f"text2cad {slug} ({vn()} VN): run ket thuc, KHONG phai abort da biet. "
               f"Autoresume dung lai, can nguoi xem.\n\n{txt.strip()[-500:]}")
            return 0
        var, why = cause
        # WHY it aborted decides whether a bigger budget is even the right fix.
        # 2026-08-19: brief died at 50/60 turns with error_during_execution —
        # the relay cut a stream three times running and the proxy gave up.
        # Doubling turns there fixes nothing and spends a restart on a wrong
        # diagnosis, so only grow the budget when the phase actually ran out.
        phase = var.split("_")[0].lower()
        subtype = ""
        try:
            rj = json.loads((HERE / "out" / slug / "run.json").read_text(encoding="utf-8"))
            e = rj.get(phase) or {}
            subtype = e.get("subtype") or ""
            why = (f"{phase} {'het luot' if subtype == 'error_max_turns' else subtype}"
                   f" ({e.get('num_turns')}/{e.get('max_turns')} luot)")
        except Exception:                                        # noqa: BLE001
            pass
        # Three different failures wear the same ABORT line, and each needs a
        # different budget raised:
        #   error_max_turns        -> ran out of TURNS  -> double the turn cap
        #   error_during_execution -> network died      -> change nothing
        #   turns=None subtype=None-> ran out of TIME   -> raise the wall clock
        # run_phase builds a synthetic dict on TimeoutExpired, so a phase that
        # timed out has neither field. On 2026-08-19 brief died at 7200.1s with
        # 75/120 turns used and this script doubled the turn cap to 240 — a
        # budget that was never the constraint, while the wall stayed put.
        timed_out = subtype in (None, "") and (e.get("num_turns") is None)
        if timed_out:
            why = f"{phase} het GIO ({e.get('wall_s')}s)"
        grow = (not timed_out) and subtype != "error_during_execution"
        if left <= 0:
            tg(f"text2cad {slug} ({vn()} VN): {why} LAN NUA, het luot tu khoi dong lai. "
               f"Can ban quyet dinh.")
            return 1
        if timed_out:
            env["PHASE_TIMEOUT_MULT"] = str(int(env["PHASE_TIMEOUT_MULT"]) * 2)
        elif grow:
            env[var] = str(int(env[var]) * 2)
        left -= 1
        newlog = f"logs/{slug}-resume{9 - left}.log"
        how = (f"tu chay lai voi PHASE_TIMEOUT_MULT={env['PHASE_TIMEOUT_MULT']}" if timed_out
               else f"tu chay lai voi {var}={env[var]}" if grow
               else "tu chay lai NGUYEN ngan sach (loi mang, khong phai het luot)")
        tg(f"text2cad {slug} ({vn()} VN): {why} -> {how}. "
           f"Con {left} luot. log={newlog}")
        e = {**os.environ, **env}
        with (HERE / newlog).open("w") as fh:
            subprocess.Popen([str(HERE / "text2cad"), prompt, "--slug", slug],
                             cwd=HERE, env=e, stdout=fh, stderr=subprocess.STDOUT,
                             start_new_session=True)
        subprocess.Popen([str(HERE / "cycle_alert.py"), newlog, "--stall-min", "30"],
                         cwd=HERE, stdout=(HERE / f"logs/{slug}-resume-alert.log").open("w"),
                         stderr=subprocess.STDOUT, start_new_session=True)
        log = HERE / newlog
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
