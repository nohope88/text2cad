#!/usr/bin/env python3
"""Mechanical half of /compact — snapshot what the box is actually doing.

Writes tg/handoff.md. The session prepends the narrative (what we are trying to
do and why); this script supplies the facts that must not be recalled from
memory: live processes, service states, the current cycle log tail, run.json
phase outcomes, and the working-tree diff.

Honest scope: this makes the session's STATE durable so a restart loses nothing.
It does not shrink the running session's context window — nothing outside the
terminal can do that.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "tg" / "handoff.md"


def sh(cmd: str, limit: int = 4000) -> str:
    r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()[:limit] or "(nothing)"


def latest_log() -> Path | None:
    logs = [p for p in (HERE / "logs").glob("*.log")
            if "proxy" not in p.name]          # proxy log churns; it is not a cycle
    logs.sort(key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def main() -> int:
    log = latest_log()
    parts = [
        f"# Session handoff — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "Written by /compact from Telegram. Facts below are read live off the box.",
        "",
        "## Pipeline / agent processes",
        "```", sh("pgrep -af 'text2cad|qwen_proxy|cycle_alert|autoresume' "
                 "| grep -vE 'shell-snapshots|bash -lc|tg_handoff' "
                 "| grep -vE 'tg_bridge|tg_watch' "
                 "|| echo 'no pipeline processes running'"), "```",
        "",
        "## Services",
        "```", sh("systemctl is-active tg-bridge admindash 2>&1 | paste -d' ' "
                 "<(echo -e 'tg-bridge\\nadmindash') -"), "```",
        "",
        "## Bridge",
        "```", sh(f"{HERE}/tg_mcp.py status"), "```",
    ]
    if log:
        parts += ["", f"## Cycle log — {log.name} (last 25 lines)",
                  "```", sh(f"tail -25 {log}"), "```"]
        # run.json carries the per-phase truth the log only hints at
        slug_dirs = sorted((HERE / "out").glob("*/run.json"),
                           key=lambda p: p.stat().st_mtime)
        if slug_dirs:
            rj = slug_dirs[-1]
            try:
                d = json.loads(rj.read_text())
                rows = [f"{k}: subtype={v.get('subtype')} turns={v.get('num_turns')}/"
                        f"{v.get('max_turns')} err={v.get('is_error')} wall={v.get('wall_s')}s"
                        for k, v in d.items() if isinstance(v, dict)]
                parts += ["", f"## Phases — {rj.parent.name}", "```",
                          "\n".join(rows) or "(empty)", "```"]
            except (json.JSONDecodeError, OSError):
                pass
    parts += [
        "", "## Artefacts",
        "```", sh("for d in out/*/; do b=$d/brief.md; "
                  "[ -f \"$b\" ] && printf '%s brief=%sB tbd=%s stl=%s\\n' "
                  "\"$(basename $d)\" \"$(stat -c%s $b)\" "
                  "\"$(grep -c '\\bTBD\\b' $b)\" \"$(find $d -name '*.stl' | wc -l)\"; "
                  "done | tail -10"), "```",
        "", "## Uncommitted work",
        "```", sh("git -C %s status --short 2>&1 | head -30" % HERE), "```",
    ]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size}B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
