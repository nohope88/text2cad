#!/usr/bin/env python3
"""Swap the proxy at the next phase boundary, never mid-phase.

The read timeout must come down (one request hung 2018s while the median gap was
4s), but bouncing the proxy while an agent has a request in flight kills that
request and can cost an hour of good work. So watch the run log, wait until the
phase count goes up — the only moment no agent is mid-call — and swap there.
"""
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path("/root/text2cad")
LOG = HERE / sys.argv[1]
VENV = "/root/.venvs/pi-agent-test/bin/python3"
DONE = re.compile(r"^\[\w[\w-]*\] [\d.]+s, turns=", re.M)


def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()


start = len(DONE.findall(LOG.read_text(encoding="utf-8", errors="replace"))) if LOG.is_file() else 0
print(f"armed: {start} phase(s) already finished; swapping after the next one", flush=True)
while True:
    time.sleep(10)
    if not sh("pgrep -f 'text2cad Build a rotating'"):
        print("run ended before the boundary — leaving the proxy alone", flush=True)
        break
    txt = LOG.read_text(encoding="utf-8", errors="replace") if LOG.is_file() else ""
    if len(DONE.findall(txt)) > start:
        pid = sh("ss -lptn 'sport = :8099' | grep -oP 'pid=\\K[0-9]+' | head -1")
        if pid:
            subprocess.run(["kill", pid], capture_output=True)
            time.sleep(1)
        subprocess.Popen([VENV, str(HERE / "qwen_proxy.py"), "--pin", "qwen/qwen3.8-27b"],
                         cwd=HERE, stdout=(HERE / "logs/qwen-proxy.log").open("a"),
                         stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(3)
        print("swapped at phase boundary:", sh("curl -s --max-time 5 http://127.0.0.1:8099/_proxy_stats"), flush=True)
        break
