#!/usr/bin/env python3
"""Concept image for a DISCOVER winner — a picture before any geometry exists.

    ./concept_image.py <out_dir> [--telegram]

DISCOVER picks a product from text alone, so the human has been approving (or
paying $30 to build) something they have never seen. Seedream text-to-image
turns the panel's own PITCH/MECHANISM/PARTS into a concept shot in ~30s, which
is the cheapest possible place to say "no, not that".

Writes <out_dir>/concept.png. Never enters reference/ — BUILD must chase the
CAD renders, not a picture no geometry can match.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

SECRETS = Path("/root/panda-secrets/.env")
MODEL = "bytedance-seed/seedream-4.5"

STYLE = (
    "Photorealistic studio product photo of a single FDM 3D-printed object, "
    "PETG, subtle visible horizontal layer lines, clean semi-matte plastic, "
    "crisp edges, realistic wall thickness, no printing defects. "
    "Neutral light-gray seamless background, soft studio lighting, natural "
    "contact shadows, generous negative space, neutral white balance, no color "
    "cast. The object is the hero subject, sharp, centered, filling about 80% "
    "of a 4:3 frame. No duplicates, no props, no people, no hands, no text, no "
    "logo, no watermark, no clutter.")


def load_key() -> str:
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):  # an sk-or-v1 OpenRouter key
            return line.partition("=")[2].strip().strip('"')
    raise SystemExit("no OpenRouter key in panda-secrets/.env")


def build_prompt(text: str) -> str:
    """The panel already wrote the product description — reuse it verbatim."""
    def grab(pat, default=""):
        m = re.search(pat, text, re.M)
        return m.group(1).strip() if m else default

    subject = grab(r"^PROMPT:\s*(.+)$")
    parts = grab(r"^Mechanism:.*?—\s*(.+?)\s*parts\.$")
    if not subject:
        raise SystemExit("no PROMPT: line in discover.md")
    body = f"{subject} It is made of {parts} parts." if parts else subject
    return f"{body}\n\n{STYLE}"


def generate(prompt: str, out: Path, key: str) -> None:
    body = {"model": MODEL, "prompt": prompt, "aspect_ratio": "4:3"}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/images", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "X-Title": "text2cad-concept"})
    t0 = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=300))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:500]}")
    items = r.get("data") or []
    if not items or not items[0].get("b64_json"):
        raise SystemExit(f"no image in response: {json.dumps(r)[:400]}")
    out.write_bytes(base64.b64decode(items[0]["b64_json"]))
    print(f"concept: {out} ({out.stat().st_size // 1024}KB, {round(time.time()-t0,1)}s)")


def main() -> int:
    out_dir = Path(sys.argv[1])
    text = (out_dir / "discover.md").read_text(encoding="utf-8")
    prompt = build_prompt(text)
    print("--- prompt ---\n" + prompt + "\n--------------")
    img = out_dir / "concept.png"
    generate(prompt, img, load_key())
    if "--telegram" in sys.argv:
        env = {}
        for line in Path("/root/text2cad/.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"')
        tok, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_DM", "")
        slug = re.search(r"^WINNER:\s*(\S+)", text, re.M)
        cap = f"concept (AI, chưa phải CAD): {slug.group(1) if slug else out_dir.name}"
        if tok and chat:
            os.system(f'curl -s "https://api.telegram.org/bot{tok}/sendPhoto" '
                      f'-F chat_id={chat} -F photo=@{img} -F caption="{cap}" > /dev/null')
            print("telegram: concept sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
