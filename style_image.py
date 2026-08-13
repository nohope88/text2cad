#!/usr/bin/env python3
"""Restyle a CAD render into a product shot — same geometry, real materials.

    ./style_image.py <in.png> <out.png>

Image-to-image, so the geometry comes from the actual model rather than from a
description: what you approve is what BUILD is holding itself to. The earlier
text-to-image attempt drew the nearest familiar object instead of the design,
which is exactly the failure mode an approval gate cannot have.

Never write the result into reference/ — BUILD compares against the raw CAD
renders, and a photoreal target it cannot express in geometry fails forever.
"""
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

SECRETS = Path("/root/panda-secrets/.env")
MODEL = "google/gemini-3-pro-image"

PROMPT = (
    "Photo-realistic product re-render (image-to-image) of a 3D-printed part.\n\n"
    "KEEP THE OBJECT EXACTLY: identical geometry, proportions, part layout, "
    "feature placement, hole positions and count, and the SAME camera angle and "
    "framing. Do not add, remove, reshape, straighten or embellish anything. If "
    "a feature looks odd or asymmetric, keep it odd — you are photographing this "
    "object, not improving it.\n\n"
    "The input is a CAD viewport render. Flat shading, faceted triangulation "
    "seams, and any striping or see-through patches are rendering artifacts of "
    "that viewport, not physical features: render the underlying solid cleanly "
    "and opaquely, without inventing detail to fill them.\n\n"
    "Make it look like a REAL part printed on an FDM printer: matte PLA, subtle "
    "horizontal layer lines following the print orientation, believable wall "
    "thickness, slightly softened edges, honest plastic. Keep the object's "
    "existing colours.\n\n"
    "STYLE: clean e-commerce catalog shot — seamless off-white studio backdrop, "
    "soft even lighting, natural contact shadow, product centered and sharp. No "
    "props, no hands, no text, no watermark.")


def key() -> str:
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):  # an sk-or-v1 OpenRouter key
            return line.partition("=")[2].strip().strip('"')
    raise SystemExit("no OpenRouter key in panda-secrets/.env")


def main() -> int:
    inp, out = Path(sys.argv[1]), Path(sys.argv[2])
    du = "data:image/png;base64," + base64.b64encode(inp.read_bytes()).decode()
    # the key's remaining headroom is below this model's 32k default ceiling,
    # and one image costs ~1.3k — asking for the default just 402s
    body = {"model": MODEL, "modalities": ["image", "text"], "max_tokens": 8192,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": du}}]}]}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key(),
                 "Content-Type": "application/json",
                 "X-Title": "text2cad-style"})
    t0 = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=300))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:400]}")
    imgs = (r.get("choices") or [{}])[0].get("message", {}).get("images") or []
    if not imgs:
        raise SystemExit(f"no image returned: {json.dumps(r)[:300]}")
    out.write_bytes(base64.b64decode(imgs[0]["image_url"]["url"].split(",", 1)[1]))
    print(f"styled: {out} ({out.stat().st_size // 1024}KB, {round(time.time()-t0,1)}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
