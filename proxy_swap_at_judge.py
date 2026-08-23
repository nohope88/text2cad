#!/usr/bin/env python3
"""Restart the proxy in the gap between PROPOSE and JUDGE, not during a phase.

The read timeout has to go up (a propose turn already hit 534s and BUILD carries
far more context), but bouncing the proxy mid-phase kills whatever request is in
flight — and propose-family already died that way once. So wait for every
propose lane to have reported, then swap in the same second, before the judges
open their first connection.
"""
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path("/root/text2cad")
LOG = HERE / sys.argv[1] if len(sys.argv) > 1 else HERE / "logs/game2-20260818.log"
LANES = 3
VENV = "/root/.venvs/pi-agent-test/bin/python3"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


while True:
    time.sleep(10)
    txt = LOG.read_text(encoding="utf-8", errors="replace") if LOG.is_file() else ""
    done = len(re.findall(r"^\[propose-\w+\] [\d.]+s,", txt, re.M))
    if not sh("pgrep -f 'python3 [.]/text2cad --discover'"):
        print("driver gone before the swap window — nothing to do", flush=True)
        break
    if done >= LANES or re.search(r"^\[judge-", txt, re.M):
        pid = sh("ss -lptn 'sport = :8099' | grep -oP 'pid=\\K[0-9]+' | head -1")
        if pid:
            subprocess.run(["kill", pid], capture_output=True)
            time.sleep(1)
        subprocess.Popen(
            [VENV, str(HERE / "qwen_proxy.py"), "--pin", "qwen/qwen3.8-27b",
             "--max-tokens", "64000", "--read-timeout", "2400"],
            cwd=HERE, stdout=(HERE / "logs/qwen-proxy.log").open("a"),
            stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(3)
        print(f"swapped proxy after {done}/{LANES} propose lanes reported", flush=True)
        print(sh("curl -s --max-time 5 http://127.0.0.1:8099/_proxy_stats"), flush=True)
        break
