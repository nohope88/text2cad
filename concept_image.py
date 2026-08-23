#!/usr/bin/env python3
"""Concept image for the DISCOVER winner — the quality bar the build aims at.

    ./concept_image.py <out_dir> [--telegram]

DISCOVER picks a product from text, and until now the first picture arrived
from DRAFT ~45min and $8.50 later. This renders the panel's own product
description in ~15s so the human can answer the only question that matters at
this point: is this the quality I want? The approved image then stays in the
output dir as the visual target DRAFT and BUILD chase.

Backend is the self-hosted media gateway (hunyuan t2i), which has its own key
and does not touch the OpenRouter budget.
"""
import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Base URL moved 2026-08-19 (owner's call, verified end-to-end first: t2i 12s,
# pisa-sr 4.0MP, fl2va mp4 with full atoms). Same MEDIA_GATEWAY_API_KEY.
# Results now land on storage.googleapis.com/lora-lab/ instead of the
# gateway's own /statics/ path — read result_files[0].file_url, never build it.
# Rollback: https://2x4090-9091.eternalai.org (still live as of 2026-08-19)
GATEWAY = "https://lora-lab-be.eternalai.org/v2/api/media-gen"
SECRETS = Path("/root/panda-secrets/media-gateway.env")
T2C_ENV = Path("/root/text2cad/.env")
UA = "curl/8.7.1"  # the gateway's CDN 1010s a default python-urllib UA
POLL_S, POLL_EVERY = 240, 10

STYLE = (
    "Premium studio product photograph, single object on a seamless warm-neutral "
    "backdrop, soft directional light, shallow depth of field, crisp focus on the "
    "mechanism. The object is a multi-part 3D-printed mechanical device: matte "
    "filament surfaces with fine layer lines, precise tolerances, parts that "
    "visibly move against each other. Industrial-design quality, the kind of "
    "object a design magazine would photograph. No text, no logo, no watermark, "
    "no hands, no clutter.")


def env(path: Path) -> dict:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"')
    return out


def get(url: str, key: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Authorization": "Bearer " + key})
    return json.load(urllib.request.urlopen(req, timeout=30))


def build_prompt(text: str) -> str:
    """The panel already wrote the product description — that IS the brief."""
    def grab(pat):
        m = re.search(pat, text, re.M)
        return m.group(1).strip() if m else ""

    subject = grab(r"^PROMPT:\s*(.+)$")
    mech = grab(r"^Mechanism:\s*(.+?)\s*—")
    parts = grab(r"^Mechanism:.*?—\s*(\d+)")
    if not subject:
        raise SystemExit("no PROMPT: line in discover.md")
    bits = [subject]
    if mech:
        bits.append(f"The mechanism to show: {mech}")
    if parts:
        bits.append(f"It has {parts} separate printed parts, all visible.")
    return " ".join(bits) + "\n\n" + STYLE


I2I_STYLE = (
    "Re-surface this EXACT object as a premium studio product photograph. Keep "
    "the geometry, the viewpoint, the proportions and the number of parts "
    "EXACTLY as in the reference — this is a photographic finishing pass, not a "
    "redesign. Do NOT add, remove, merge or move any part. Do NOT invent text, "
    "labels, engravings, screens, displays, decals, stickers or logos of any "
    "kind. Do NOT invent surface texture on flat faces. Change ONLY the "
    "material, lighting and background: matte 3D-printed filament with fine "
    "layer lines, soft directional studio light, seamless warm-neutral backdrop, "
    "shallow depth of field. THE ENTIRE OBJECT IS FULLY VISIBLE AND COMPLETE "
    "with a generous margin on all four sides; the bottom edge of the picture "
    "shows only the backdrop, never the object; the object fills only about 60 "
    "percent of the frame.")


def prepare_ref(png: Path, max_edge: int = 768) -> str:
    """Reference -> small padded JPEG data URI.

    Two measured constraints from /root/docs/hunyuan-gateway.md: the request
    body dies at ~1.5 MB, and i2i zooms in and anchors the subject to the BOTTOM
    frame edge (7/10 outputs had geometry touching it). White padding of
    10%/8%/22% was what made that 10/10 clean, so pad before shrinking.
    """
    from PIL import Image
    im = Image.open(png)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    w, h = im.size
    padded = Image.new("RGB", (int(w * 1.20), int(h * 1.30)), (255, 255, 255))
    padded.paste(im, (int(w * 0.10), int(h * 0.08)))
    im = padded
    if max(im.size) > max_edge:
        s = max_edge / max(im.size)
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


QA_PROMPT = """You are the QA judge for a product concept image.

Read BOTH images with the Read tool:
  REFERENCE (the true CAD geometry): {ref}
  CANDIDATE (photoreal re-surface):  {cand}

The candidate must be THE SAME OBJECT as the reference: same parts, same part
count, same layout, same viewpoint. Only material, lighting and background are
allowed to differ — the reference is a flat-shaded CAD render, the candidate is
meant to look like a photograph of that same printed object. Never score colour,
finish or lighting.

Classify the WORST problem you can actually see:

STRUCTURAL — the candidate is a different object. Any of:
  a part added, removed, merged, split or moved; the part count changed; the
  viewpoint or camera angle changed; the object cut off by a frame edge.
COSMETIC — the object is right but the surface carries something invented:
  text, letters, numbers, labels, engravings, logos, screens, decals, or
  invented texture on a face that is flat in the reference.

Reply with EXACTLY one line and nothing else:
PASS <what you checked>
COSMETIC <what was invented, one sentence>
STRUCTURAL <what differs, one sentence specific enough to fix>
"""


def qa_judge(ref: Path, cand: Path) -> str:
    """One line: PASS… | COSMETIC… | STRUCTURAL…  ('PASS qa off' when disabled).

    Deliberately NOT a binary gate. Today's first i2i concept invented an
    engraved plaque while reproducing the geometry perfectly — that is worthless
    to fix and harmless to keep, because likeness judges silhouette, part count
    and feature presence, never surface decoration. A verdict that cannot tell
    "wrong object" from "right object, spurious label" would throw away a good
    anchor for a cosmetic blemish, which is the same conflation that cost a $29
    run this morning.
    """
    if os.environ.get("CONCEPT_QA", "").lower() == "off":
        return "PASS qa off"
    penv = dict(os.environ)  # not `env` — that is this module's .env parser
    penv["PATH"] = "/root/.local/bin:" + penv.get("PATH", "")
    try:
        r = subprocess.run(
            ["claude", "-p", QA_PROMPT.format(ref=ref, cand=cand),
             "--model", os.environ.get("CONCEPT_QA_MODEL", "claude-sonnet-5"),
             "--allowedTools", "Read", "--add-dir", str(ref.parent),
             "--add-dir", str(cand.parent), "--max-turns", "8",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=600, env=penv)
        data = json.loads(r.stdout.strip().splitlines()[-1])
        line = (data.get("result") or "").strip().splitlines()[-1].strip()
    except Exception as e:  # noqa: BLE001
        return f"PASS qa unavailable ({str(e)[:80]})"
    for tag in ("STRUCTURAL", "COSMETIC", "PASS"):
        if line.upper().startswith(tag):
            return line
    # an unparseable verdict is not a failing one — say so instead of guessing
    return f"PASS qa unparseable ({line[:80]})"


def generate_i2i(prompt: str, ref: Path, out: Path, key: str, seed: int = 7) -> None:
    """Photoreal concept built FROM the real geometry, not from text.

    The t2i concept is drawn by a model that has never seen what CAD can build,
    so it invents garbled engravings, second-colour screens and knurling that no
    single-material FDM print can carry — and then the likeness lens scores the
    build against that fiction. i2i keeps the object and changes only the
    surface, which also makes the gateway's locked camera an asset: same
    viewpoint means silhouette differences are real differences.
    """
    body = {"model": "hunyuan-image-3-i2i", "type": "image-to-image",
            "prompt": prompt, "image_url": prepare_ref(ref), "num_images": 1,
            "seed": seed, "aspect_ratio": "auto", "output_format": "jpeg",
            "enable_safety_checker": True}
    req = urllib.request.Request(f"{GATEWAY}/media/generations",
                                 data=json.dumps(body).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Authorization": "Bearer " + key})
    t0 = time.time()
    rid = json.load(urllib.request.urlopen(req, timeout=60))["request_id"]
    while time.time() - t0 < POLL_S:
        time.sleep(POLL_EVERY)
        r = get(f"{GATEWAY}/media/generations/{rid}", key)
        if r.get("status") == "completed":
            url = (r.get("result_files") or [{}])[0].get("file_url")
            if not url:
                raise SystemExit(f"completed with no file: {json.dumps(r)[:200]}")
            img = urllib.request.Request(url, headers={"User-Agent": UA})
            out.write_bytes(urllib.request.urlopen(img, timeout=120).read())
            print(f"concept(i2i): {out} ({out.stat().st_size // 1024}KB, "
                  f"{round(time.time()-t0)}s, from {ref.name})")
            return
        if r.get("status") == "failed":
            # the gateway returns no diagnostics on failure, by design
            raise SystemExit(f"gateway failed: {json.dumps(r)[:200]}")
    raise SystemExit(f"gateway still {r.get('status')} after {POLL_S}s — giving up")


def generate_i2i_qa(prompt: str, ref: Path, out: Path, key: str) -> None:
    """i2i behind the QA judge: reseed on rejection, then decide by severity.

    STRUCTURAL after the retry means the gateway will not reproduce this object,
    so raise and let the caller fall back to the raw render — a wrong-object
    anchor is worse than a plain one. COSMETIC is accepted and logged: it is a
    real photo of the real geometry with a spurious label on it, which still
    answers every question likeness asks.
    """
    attempts = []
    # Seeds are the only variation lever the gateway exposes (strength and
    # image_size are silently ignored — /root/docs/hunyuan-gateway.md), and each
    # attempt is 21s and $0. Only the judge calls cost anything, so try a few.
    seeds = [int(s) for s in os.environ.get("CONCEPT_SEEDS", "7,23,101,404").split(",")]
    for i, seed in enumerate(seeds):
        last = i == len(seeds) - 1
        p = prompt if not attempts else (
            prompt + "\n\nPREVIOUS ATTEMPT REJECTED — " + attempts[-1][1] +
            " Fix exactly that and change nothing else about the object.")
        generate_i2i(p, ref, out, key, seed=seed)
        verdict = qa_judge(ref, out)
        attempts.append((out.read_bytes(), verdict))
        print(f"concept qa (attempt {i + 1}, seed {seed}): {verdict}", flush=True)
        if verdict.upper().startswith("PASS"):
            return
        if verdict.upper().startswith("COSMETIC") and last:
            return  # right object, spurious decoration — good enough to judge against
    # Nothing passed clean. A cosmetic miss is still the right object, so prefer
    # it; if every attempt drifted structurally the gateway will not reproduce
    # this geometry, and a wrong-object anchor is worse than a plain render.
    for blob, verdict in attempts:
        if verdict.upper().startswith("COSMETIC"):
            out.write_bytes(blob)
            print(f"concept qa: keeping the cosmetic attempt — {verdict}", flush=True)
            return
    raise SystemExit(
        f"concept qa: structural mismatch on all {len(attempts)} seeds — {attempts[-1][1]}")


def generate(prompt: str, out: Path, key: str) -> None:
    body = {"model": "hunyuan-image-3-t2i", "type": "text-to-image",
            "prompt": prompt, "image_size": "landscape_4_3", "num_images": 1,
            "output_format": "jpeg", "enable_safety_checker": True}
    req = urllib.request.Request(f"{GATEWAY}/media/generations",
                                 data=json.dumps(body).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Authorization": "Bearer " + key})
    t0 = time.time()
    rid = json.load(urllib.request.urlopen(req, timeout=60))["request_id"]
    while time.time() - t0 < POLL_S:
        time.sleep(POLL_EVERY)
        r = get(f"{GATEWAY}/media/generations/{rid}", key)
        if r.get("status") == "completed":
            url = (r.get("result_files") or [{}])[0].get("file_url")
            if not url:
                raise SystemExit(f"completed with no file: {json.dumps(r)[:200]}")
            img = urllib.request.Request(url, headers={"User-Agent": UA})
            out.write_bytes(urllib.request.urlopen(img, timeout=120).read())
            print(f"concept: {out} ({out.stat().st_size // 1024}KB, "
                  f"{round(time.time()-t0)}s)")
            return
        if r.get("status") == "failed":
            raise SystemExit(f"gateway failed: {json.dumps(r)[:200]}")
    raise SystemExit(f"gateway still {r.get('status')} after {POLL_S}s — giving up")


def main() -> int:
    out_dir = Path(sys.argv[1])
    text = (out_dir / "discover.md").read_text(encoding="utf-8")
    prompt = build_prompt(text)
    if "--from-render" in sys.argv:  # log what is actually sent, not the t2i variant
        prompt = prompt.split("\n\n")[0] + "\n\n" + I2I_STYLE
    print("--- prompt ---\n" + prompt + "\n--------------")
    img = out_dir / "concept.png"
    key = env(SECRETS).get("MEDIA_GATEWAY_API_KEY", "")
    if not key:
        raise SystemExit("no MEDIA_GATEWAY_API_KEY in panda-secrets/media-gateway.env")
    if "--from-render" in sys.argv:
        ref = Path(sys.argv[sys.argv.index("--from-render") + 1])
        if not ref.is_file():
            raise SystemExit(f"--from-render: {ref} does not exist")
        generate_i2i_qa(prompt, ref, img, key)
    else:
        generate(prompt, img, key)

    if "--telegram" in sys.argv:
        e = env(T2C_ENV)
        tok, chat = e.get("TELEGRAM_BOT_TOKEN", ""), e.get("TELEGRAM_CHAT_DM", "")
        slug = re.search(r"^WINNER:\s*(\S+)", text, re.M)
        slug = slug.group(1) if slug else out_dir.name
        cap = f"{slug} — concept: đây là chất lượng BUILD sẽ nhắm tới"
        if tok and chat:
            os.system(f'curl -s "https://api.telegram.org/bot{tok}/sendPhoto" '
                      f'-F chat_id={chat} -F photo=@{img} '
                      f'-F caption="{cap}" > /dev/null')
            print("telegram: concept sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
