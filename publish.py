#!/usr/bin/env python3
"""Publish a finished part to Panda Social as a DRAFT design.

    ./publish.py <slug>

Flow (all inside the VM): upload hero/parts renders via admindash /uploads
(CDN) -> bin/importdesign inserts the design with status=draft -> Telegram
tells Tam to review. The human confirmation IS the draft->public flip in
admindash — this script never publishes anything publicly.

Needs in .env: ADMIN_TOKEN (admindash bearer), PANDA_OWNER_ID (24-hex user id
that owns imported designs). Missing either -> graceful skip.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADMINDASH = os.environ.get("ADMINDASH_URL", "http://localhost:8090")


def load_env():
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def upload(path: Path, token: str) -> str:
    r = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                        "-F", f"file=@{path}", f"{ADMINDASH}/api/uploads"],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"https?://[^\s\"']+", r.stdout)
    if not m:
        raise SystemExit(f"upload failed for {path.name}: {r.stdout[:200]}")
    return m.group(0)


def telegram(text: str) -> None:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_DM", "")
    if tok and chat:
        subprocess.run(["curl", "-s", f"https://api.telegram.org/bot{tok}/sendMessage",
                        "-d", f"chat_id={chat}", "--data-urlencode", f"text={text}"],
                       capture_output=True, timeout=30)


def main() -> int:
    load_env()
    slug = sys.argv[1]
    out_dir = HERE / "out" / slug
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    owner = os.environ.get("PANDA_OWNER_ID", "").strip()
    if not token or not owner:
        print("publish: skipped — set ADMIN_TOKEN and PANDA_OWNER_ID in .env")
        return 0
    if (out_dir / "published.json").is_file():
        print("publish: already published — skip")
        return 0
    brief = (out_dir / "brief.md").read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", brief, re.M)
    title = m.group(1).strip() if m else ""
    if m and title.lower() == "title":  # briefs often start with a literal "# Title" label
        rest = brief[m.end():].lstrip().splitlines()
        title = next((l.strip() for l in rest if l.strip() and not l.startswith("#")), "")
    title = (title or slug.replace("-", " ").title())[:120]
    paras = [p.strip() for p in brief.split("\n\n") if p.strip() and not p.startswith("#")]
    desc = re.sub(r"[*_`]", "", paras[0])[:500] if paras else title
    run = json.loads((out_dir / "run.json").read_text(encoding="utf-8")) \
        if (out_dir / "run.json").is_file() else {}
    prompt = run.get("prompt", "")

    thumbs = []
    for name in ("hero.png", "parts.png"):
        p = out_dir / name
        if not p.is_file():  # fall back to review renders
            alt = out_dir / f"{slug}_review" / ("_assembled.png" if name == "hero.png" else "_qa.png")
            p = alt if alt.is_file() else p
        if p.is_file():
            thumbs.append(upload(p, token))
    if not thumbs:
        raise SystemExit("publish: no renders found to upload")

    r = subprocess.run([str(HERE / "bin" / "importdesign"),
                        "-title", title, "-desc", desc, "-owner", owner,
                        "-thumbs", ",".join(thumbs), "-prompt", prompt,
                        "-tags", "text2cad,3d-print"],
                       capture_output=True, text=True, timeout=120,
                       cwd=os.environ.get("BACKEND_DIR", "/root/panda-social-backend"))
    if r.returncode != 0:
        raise SystemExit(f"importdesign failed: {r.stderr[-300:]}")
    info = json.loads(r.stdout.strip().splitlines()[-1])
    (out_dir / "published.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    telegram(f"📦 text2cad DRAFT imported: {title}\nid={info['id']} status={info['status']}\n"
             f"Duyệt trong admindash → đổi status sang public để lên feed.")
    print(f"published as draft: {info}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
