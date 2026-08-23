#!/usr/bin/env python3
"""On-demand how-to video for a finished part, photoreal.

    ./gen_howto_video.py <slug> [--dry-run] [--reuse-frame <url>]

Different job from gen_demo_video.py: that one fires DETACHED at draft time off
whatever render exists. This one is the hand-directed product demo — you write
the two prompts, it runs the whole chain and telegrams the clip.

    FE render -> hunyuan-image-3-i2i (photoreal) -> pisa-sr 4mp -> fl2va -> Telegram

Every stage hands the next stage the gateway's OWN result URL, so nothing is
re-uploaded mid-chain. Measured 2026-08-17: i2i ~16s, pisa-sr ~10s (-> 2664x1496),
fl2va ~161s. All $0 — it is the self-hosted 2x4090 gateway, not a paid API.

Spec lives at out/<slug>/howto.json; run with no spec to get a template.
Gateway params + failure modes: /root/docs/{hunyuan,minimax-h3}-gateway.md
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Base URL moved 2026-08-19 (owner's call, verified end-to-end first: t2i 12s,
# pisa-sr 4.0MP, fl2va mp4 with full atoms). Same MEDIA_GATEWAY_API_KEY.
# Results now land on storage.googleapis.com/lora-lab/ instead of the
# gateway's own /statics/ path — read result_files[0].file_url, never build it.
# Rollback: https://2x4090-9091.eternalai.org (still live as of 2026-08-19)
GATEWAY = "https://lora-lab-be.eternalai.org/v2/api/media-gen"
ADMINDASH = os.environ.get("ADMINDASH_URL", "http://localhost:8090")

TEMPLATE = {
    "frame": "howto_frame_src.png",
    "seed": 7,
    "durations": [12, 10, 6],
    "out": "howto.mp4",
    "i2i_prompt": (
        "A premium studio product photograph of this exact machine, real and "
        "physical. Keep the geometry, the viewpoint, the proportions, the part "
        "count and the COLOURS exactly as in the reference: <name every part by "
        "its colour, read off THIS frame>. Render it as a real FDM 3D print: "
        "matte filament with fine visible layer lines, slight extrusion sheen, "
        "crisp printed edges, tiny seams where parts meet. Replace the technical "
        "grid floor with a seamless warm-neutral studio backdrop, soft "
        "directional lighting, a soft contact shadow, shallow depth of field. "
        "Photographic realism, not CGI, not a render. Do not add or remove any "
        "part. No text, no labels, no logos, no hands. The entire object is "
        "fully visible with a generous margin on all four sides."
    ),
    "video_prompt": (
        "Product how-to demo of this exact 3D-printed desk machine on a studio "
        "surface, camera slowly orbiting a few degrees and pushing in slightly. "
        "Four beats.\n"
        "BEAT 1 - <action, naming each moving part BY ITS COLOUR>. Narration: "
        "'...'\n"
        "BEAT 2 - ... \nBEAT 3 - ... \nBEAT 4 - ...\n"
        "Keep the exact colours and materials of the first frame - do not "
        "recolour anything. Real matte FDM print surface with fine layer lines. "
        "No text overlays, no logos, no extra objects. Soundscape: <real "
        "mechanical sound>. Quiet room ambience, no music."
    ),
    "caption": "<product> - how to use",
}


def env_file(path) -> dict:
    d = {}
    p = Path(path)
    if not p.is_file():
        return d
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip().strip('"')
    return d


GW = env_file("/root/panda-secrets/media-gateway.env").get("MEDIA_GATEWAY_API_KEY", "")
T2C = env_file(HERE / ".env")


def curl_json(args, timeout=90) -> dict:
    # Always shell out to curl: Cloudflare 1010s a python-urllib UA on this
    # gateway. The binary's own curl/8.x UA is what gets through.
    r = subprocess.run(["curl", "-s", "--max-time", str(timeout)] + args,
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return {"_raw": r.stdout[:200]}


def upload_cdn(path: Path) -> str:
    """Local file -> CDN url, via admindash (same path publish.py uses)."""
    token = T2C.get("ADMIN_TOKEN") or env_file(
        "/root/panda-secrets/admindash.env").get("ADMIN_TOKEN", "")
    r = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                        "-F", f"file=@{path}", f"{ADMINDASH}/api/uploads"],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"https?://[^\s\"']+", r.stdout)
    if not m:
        raise SystemExit(f"upload failed for {path.name}: {r.stdout[:200]}")
    return m.group(0)


def submit_poll(body: dict, label: str, max_wait: int = 1800):
    """Submit one gateway job, poll to completion, return its result file_url."""
    j = curl_json(["-X", "POST", f"{GATEWAY}/media/generations",
                   "-H", f"Authorization: Bearer {GW}",
                   "-H", "Content-Type: application/json", "-d", json.dumps(body)])
    rid, st = j.get("request_id"), j.get("status")
    print(f"[{label}] id={rid} status={st}", flush=True)
    if not rid or st == "failed":
        return None
    t0 = last = time.time()
    prog = None
    while time.time() - t0 < max_wait:
        time.sleep(5)
        r = curl_json(["-H", f"Authorization: Bearer {GW}",
                       f"{GATEWAY}/media/generations/{rid}"])
        if r.get("progress") != prog:
            prog, last = r.get("progress"), time.time()
            print(f"  [{label}] {r.get('status')} {prog} "
                  f"t={time.time()-t0:.0f}s", flush=True)
        if r.get("status") == "completed":
            url = (r.get("result_files") or [{}])[0].get("file_url")
            print(f"  [{label}] -> {url}", flush=True)
            return url
        if r.get("status") == "failed":
            print(f"  [{label}] FAILED (gateway gives no error detail)", flush=True)
            return None
        if time.time() - last > 900:
            print(f"  [{label}] stalled >15min", flush=True)
            return None
    print(f"  [{label}] timed out", flush=True)
    return None


def telegram(video: Path, caption: str) -> None:
    tok, chat = T2C.get("TELEGRAM_BOT_TOKEN", ""), T2C.get("TELEGRAM_CHAT_DM", "")
    if not (tok and chat):
        print("  telegram: no token/chat in .env — skipped", flush=True)
        return
    s = subprocess.run(["curl", "-s", f"https://api.telegram.org/bot{tok}/sendVideo",
                        "-F", f"chat_id={chat}", "-F", f"video=@{video}",
                        "-F", f"caption={caption}"], capture_output=True, text=True)
    print("  telegram:", "ok" if '"ok":true' in s.stdout else s.stdout[:200])


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    slug = args[0]
    dry = "--dry-run" in args
    reuse = None
    if "--reuse-frame" in args:
        reuse = args[args.index("--reuse-frame") + 1]

    # --dir: text2game keeps its runs in its own repo and the chain is the same
    out_dir = HERE / "out" / slug
    if "--dir" in sys.argv:
        out_dir = Path(sys.argv[sys.argv.index("--dir") + 1]).resolve()
    if not out_dir.is_dir():
        print(f"no such run: {out_dir}")
        return 1
    spec_path = out_dir / "howto.json"
    if not spec_path.is_file():
        spec_path.write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")
        print(f"wrote a template to {spec_path} — fill in the two prompts "
              f"(name every part BY ITS COLOUR, read off the frame you are "
              f"feeding in) and re-run.")
        return 2
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out = out_dir / spec.get("out", "howto.mp4")
    shot = out.with_name(out.stem + "_frame.png")

    if not GW:
        print("no MEDIA_GATEWAY_API_KEY")
        return 1

    # 0. the source frame. An FE render (fe_colors.py / render_three43_fe.mjs)
    #    is the right input — it carries the viewer's real part colours.
    if reuse:
        frame = reuse
        print(f"[frame] reusing {frame}")
    else:
        src = spec.get("frame_url")
        if not src:
            local = out_dir / spec.get("frame", "howto_frame_src.png")
            if not local.is_file():
                print(f"missing frame {local} (or set frame_url in the spec)")
                return 1
            src = upload_cdn(local)
            print(f"[frame] {local.name} -> {src}")

        if dry:
            print(json.dumps({"i2i_prompt": spec["i2i_prompt"],
                              "video_prompt": spec["video_prompt"],
                              "frame": src}, indent=2))
            return 0

        # 1. CAD/FE render -> photoreal. Straight i2i: no concept step, no QA
        #    gate. Routing this through the QA-gated concept flow rejects seeds
        #    for "structural change" that is really just photorealism.
        photo = submit_poll({"model": "hunyuan-image-3-i2i", "type": "image-to-image",
                             "prompt": spec["i2i_prompt"], "image_url": src,
                             "num_images": 1, "seed": spec.get("seed", 7),
                             "aspect_ratio": "16:9", "output_format": "jpeg",
                             "enable_safety_checker": True}, "i2i")
        if not photo:
            print("i2i failed")
            return 1

        # 2. upscale — i2i tops out near 1MP and ignores image_size.
        up = submit_poll({"model": "pisa-sr", "image_url": photo,
                          "output_resolution": "4mp", "output_format": "png"},
                         "pisa-sr")
        if not up:
            print("  pisa-sr failed — falling back to the un-upscaled i2i frame")
        frame = up or photo
        subprocess.run(["curl", "-s", "-o", str(shot), frame], check=False)
        print(f"  frame saved {shot} ({shot.stat().st_size//1024}KB)", flush=True)

    if dry:
        print(json.dumps({"video_prompt": spec["video_prompt"], "frame": frame},
                         indent=2))
        return 0

    # 3. photoreal frame -> video.
    for dur in spec.get("durations", [12, 10, 6]):
        vid = submit_poll({"model": "minimax/minimax-h3-fl2va",
                           "type": "image-to-video", "image_url": frame,
                           "prompt": spec["video_prompt"], "duration": dur,
                           "aspect_ratio": "16:9", "resolution": "480p"},
                          f"fl2va{dur}s")
        if not vid:
            continue
        subprocess.run(["curl", "-s", "-o", str(out), vid], check=False)
        # Scan the WHOLE file: moov/mp4a sit at the END (byte 3.1M in a 3.1MB
        # clip). A head-only check rejects perfectly good videos.
        data = out.read_bytes()
        ok = b"avc1" in data and b"mp4a" in data
        print(f"  saved {out} ({len(data)//1024}KB) avc1={b'avc1' in data} "
              f"mp4a={b'mp4a' in data}", flush=True)
        if ok:
            telegram(out, spec.get("caption", slug))
            return 0
        print("  missing an atom — trying a shorter clip", flush=True)
    print("video stage exhausted")
    return 1


if __name__ == "__main__":
    sys.exit(main())
