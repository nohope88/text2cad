#!/usr/bin/env python3
"""Live watchdog for ONE text2cad cycle — telegrams the human when it breaks.

    ./cycle_alert.py <logfile> [--stall-min 20] [--poll 30]

Different granularity from watchdog.sh: that is a daily dead-man check on the
heartbeat file (silent >28h). This one rides a single run and alerts within a
minute, so a 13h unattended cycle cannot fail silently at hour two.

Alerts ONCE per condition, never spams:
  - driver process exits          -> cycle over (shipped, or died)
  - failure signature in the log  -> quota death, output cap, STUCK, abort
  - a phase records is_error      -> read from run.json, with its subtype
  - everything goes quiet         -> no log AND no agent-transcript write in
                                     --stall-min minutes; this is the one that
                                     catches a hung gateway, since a stalled
                                     phase writes nothing and looks identical
                                     to a healthy long phase from the log alone
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Line-by-line, never `set -a; source .env` — MONGODB_URI's value breaks shell
# parsing and takes the whole file's exports down with it.
ENV = {}
for _line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        ENV[_k.strip()] = _v.strip().strip('"')

FAIL = re.compile(
    r"usage limit|limit reached|rate limit|"
    r"exceeded the \d+ output token|output token maximum|"
    r"Traceback \(most recent|"
    r"ABORT|aborting|abandon|"
    r"\bSTUCK\b|timed out after|"
    r"GATE: FAIL|no legal|connection error|502 Bad Gateway|Internal Server Error",
    re.I)

# Transcript dirs the phase agents write to — their mtime is the only liveness
# signal during a long phase, because the cycle log only writes at boundaries.
TRANSCRIPTS = Path("/root/.claude/projects")


def tg(text: str) -> None:
    tok, chat = ENV.get("TELEGRAM_BOT_TOKEN", ""), ENV.get("TELEGRAM_CHAT_DM", "")
    if not (tok and chat):
        print(f"[no telegram creds] {text}", flush=True)
        return
    r = subprocess.run(["curl", "-s", "--max-time", "30",
                        f"https://api.telegram.org/bot{tok}/sendMessage",
                        "-d", f"chat_id={chat}",
                        "--data-urlencode", f"text={text}"],
                       capture_output=True, text=True)
    ok = '"ok":true' in r.stdout
    print(f"[alert sent ok={ok}] {text[:120]}", flush=True)


def driver_alive() -> bool:
    # Must match BOTH launch styles: a human runs `./text2cad ...` while
    # autoresume respawns it as `/root/text2cad/text2cad ...`. On 2026-08-19 the
    # relative-only pattern made a freshly spawned watchdog declare the run dead
    # in its first second, fire a false "cycle ENDED", and exit — leaving the
    # night with no Telegram cover at all. The [t] still blocks a self-match.
    r = subprocess.run(["pgrep", "-f", r"python3 .*[t]ext2cad (--discover|[A-Z\"'])"],
                       capture_output=True, text=True)
    return r.returncode == 0


def newest_activity(log: Path) -> float:
    """Most recent write across the cycle log and every phase transcript."""
    newest = log.stat().st_mtime if log.is_file() else 0.0
    for d in TRANSCRIPTS.glob("-root-text2cad*"):
        for f in d.glob("*.jsonl"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
    return newest


BANNED = re.compile(r"\b(paper|cardboard|card stock|companion app|smartphone|"
                    r"battery|batteries|touchscreen|QR code)\b", re.I)
REQUIRED = ("PITCH", "GENRE", "PLAYERS", "TIME", "MECHANISM", "PARTS", "NEAREST",
            "WHY-NOBODY-HAS-THIS", "PROMPT")


def bad_candidates(disc: Path) -> list:
    """Content faults a phase-level watchdog cannot see.

    On 2026-08-18 an auto-routed run finished every phase "successfully" while
    emitting word salad and CJK into cand_*.md, and proposed games made of
    folded paper — which the prompt bans outright. is_error was False and
    nothing in the log looked wrong, so the run would have reached the judges
    before anyone noticed. Read what was actually written.
    """
    out = []
    for f in sorted(disc.glob("cand_*.md")):
        try:
            t = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in re.split(r"^CANDIDATE:", t, flags=re.M)[1:]:
            slug = (block.strip().splitlines() or [""])[0].strip()[:40]
            miss = [k for k in REQUIRED if not re.search(rf"^{re.escape(k)}:", block, re.M)]
            if miss:
                out.append(f"{f.name}/{slug}: thiếu field {miss}")
            hits = sorted(set(m.group(0).lower() for m in BANNED.finditer(block)))
            if hits:
                out.append(f"{f.name}/{slug}: vi phạm luật linh kiện — {hits}")
            # Latin-script pipeline: a run of CJK means the model came apart.
            cjk = sum(1 for ch in block if "\u4e00" <= ch <= "\u9fff")
            if cjk > 8:
                out.append(f"{f.name}/{slug}: {cjk} ký tự CJK — output degenerate")
    return out


def skeleton_brief(slug_dir: Path):
    """A brief.md that exists but says nothing is worse than none at all.

    2026-08-19: the step-0 rule finally got a file on disk, but the file was the
    SKELETON — every dimension TBD, Parts table empty. The pipeline only checks
    is_file(), so a skeleton passes the gate and BUILD gets a spec with no
    numbers. Loud failure became silent failure. Warn on it.
    """
    f = slug_dir / "brief.md"
    if not f.is_file():
        return None
    t = f.read_text(encoding="utf-8", errors="replace")
    tbd = len(re.findall(r"\bTBD\b", t))
    if tbd >= 8 or "Status: SKELETON" in t:
        return f"{f} vẫn là KHUNG ({tbd} chỗ TBD, {len(t)}B) — BUILD sẽ không có số đo"
    return None


def errored_phases(out_root: Path) -> dict:
    """Phases whose run.json entry says is_error, keyed slug/phase."""
    bad = {}
    for rj in out_root.glob("*/run.json"):
        try:
            d = json.loads(rj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for name, e in d.items():
            if isinstance(e, dict) and e.get("is_error"):
                bad[f"{rj.parent.name}/{name}"] = e
    return bad


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    log = Path(sys.argv[1])
    argv = sys.argv[2:]
    stall_min = float(argv[argv.index("--stall-min") + 1]) if "--stall-min" in argv else 20.0
    poll = float(argv[argv.index("--poll") + 1]) if "--poll" in argv else 30.0
    out_root = HERE / "out"

    fired = set()
    pos = log.stat().st_size if log.is_file() else 0   # only NEW lines
    known_err = set(errored_phases(out_root))          # pre-existing, not ours
    print(f"watching {log} (stall={stall_min}min poll={poll}s), "
          f"{len(known_err)} pre-existing errors ignored", flush=True)

    while True:
        time.sleep(poll)

        # 1. new failure signatures in the log
        if log.is_file() and log.stat().st_size > pos:
            with log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            for line in chunk.splitlines():
                if FAIL.search(line):
                    key = hashlib.md5(line.strip().encode()).hexdigest()[:12]
                    if key not in fired:
                        fired.add(key)
                        tg(f"⚠️ text2cad qwen cycle — failure signature\n\n{line.strip()[:600]}"
                           f"\n\nlog: {log}")

        # 2. a phase recorded is_error
        for name, e in errored_phases(out_root).items():
            if name not in known_err and f"err:{name}" not in fired:
                fired.add(f"err:{name}")
                tg(f"⚠️ text2cad qwen cycle — phase failed\n\n{name}\n"
                   f"subtype={e.get('subtype')} turns={e.get('num_turns')}/"
                   f"{e.get('max_turns')} wall={e.get('wall_s')}s\n\nlog: {log}")

        # 3. what the phases actually WROTE (a clean phase can emit garbage)
        for fault in bad_candidates(HERE / "out" / "_discover"):
            key = "cand:" + hashlib.md5(fault.encode()).hexdigest()[:10]
            if key not in fired:
                fired.add(key)
                tg(f"CANDIDATE HONG - text2cad board game\n\n{fault}\n\n"
                   f"Phase khong bao loi, nhung noi dung ghi ra la sai. "
                   f"log: {log}")

        # 3b. brief exists but is still a skeleton
        for d in out_root.glob("*/brief.md"):
            fault = skeleton_brief(d.parent)
            if fault and ("skel:" + d.parent.name) not in fired:
                fired.add("skel:" + d.parent.name)
                tg(f"CANH BAO text2cad — brief.md chi la KHUNG\n\n{fault}\n\n"
                   f"Phase co the ket thuc 'thanh cong' nhung BUILD se dung tu spec rong.")

        # 4. everything quiet — the hung-gateway case
        quiet_min = (time.time() - newest_activity(log)) / 60
        if quiet_min > stall_min and "stall" not in fired:
            fired.add("stall")
            tg(f"⚠️ text2cad qwen cycle — STALLED\n\nNo log or agent-transcript "
               f"write in {quiet_min:.0f} min. Gateway may be hung.\n\nlog: {log}")

        # 5. driver gone — always the last word, then stop watching
        if not driver_alive():
            tail = ""
            if log.is_file():
                tail = "\n".join(log.read_text(encoding="utf-8",
                                               errors="replace").splitlines()[-12:])
            shipped = "SHIP" in tail or "publish" in tail.lower()
            tg(f"{'✅' if shipped else '🛑'} text2cad qwen cycle ENDED "
               f"({'shipped' if shipped else 'no ship line — check'})\n\n"
               f"last lines:\n{tail[-1200:]}")
            return 0


if __name__ == "__main__":
    sys.exit(main())
