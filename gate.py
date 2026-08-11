#!/usr/bin/env python3
"""Printability gate: trimesh mesh checks + optional OrcaSlicer CLI slice.

Usage: python3 gate.py <out_dir> [--no-slice]
Writes <out_dir>/gate.json and prints a one-line verdict.
Mesh/slice logic mirrors minimax-panda's evaluate.py so scores stay comparable.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

OVERHANG_FAIL_PCT = 50.0
# 0 = no limit (Tam 2026-08-11: print time is the buyer's concern for
# digital-download products; set per-run only if selling physical prints)
PRINT_MIN_MAX = int(os.environ.get("PRINT_MIN_MAX", "0") or "0")


def lint_code(out_dir: Path) -> list:
    """Graduated lessons enforced as hard checks — never advisory notes."""
    issues = []
    for name in ("main.py", "params.py"):
        p = out_dir / name
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        # Lesson (Huggy 2026-08-11, GRADUATED): blanket fillet over every edge
        # direction of a box makes OCCT build spherical vertex-blends that
        # tessellate into phantom sliver bodies. Corner rounds belong in the
        # 2D profile (blocks/cadblocks.rounded_box), directional fillets after.
        if re.search(r"\.edges\(\s*\)\s*\.(fillet|chamfer)\(", src):
            issues.append(f"lint:{name}: blanket .edges().fillet/chamfer — use "
                          f"profile-baked corner rounds (blocks/cadblocks.rounded_box) "
                          f"or directional selectors ('|Z', '>Z')")
    return issues


def mesh_stats(stl_path: Path) -> dict:
    import trimesh

    m = trimesh.load(str(stl_path), force="mesh")
    areas = m.area_faces
    nz = m.face_normals[:, 2]
    overhang = float(areas[nz < -0.7071].sum() / areas.sum() * 100.0) if areas.sum() > 0 else 0.0
    try:
        bodies = len(m.split(only_watertight=False))
    except Exception:
        bodies = 1
    return {"watertight": bool(m.is_watertight), "volume_mm3": float(abs(m.volume)),
            "bodies": max(1, bodies), "overhang_pct": round(overhang, 2),
            "bbox_mm": [round(x, 1) for x in (m.bounds[1] - m.bounds[0]).tolist()]}


def slice_stl(stl_path: Path) -> dict:
    """ORCA_PROFILE = "machine.json;process.json;filament.json" (full paths).
    OrcaSlicer 2.4 CLI has no -o; gcode lands in --outputdir as plate_*.gcode."""
    cli = os.environ.get("ORCASLICER_CLI", "").strip()
    profile = os.environ.get("ORCA_PROFILE", "").strip()
    if not cli or not profile:
        return {"sliced": None}
    parts = [p.strip() for p in profile.split(";") if p.strip()]
    if len(parts) != 3:
        return {"sliced": False, "slice_error": "ORCA_PROFILE needs machine;process;filament"}
    with tempfile.TemporaryDirectory() as td:
        cmd = [cli, "--load-settings", f"{parts[0]};{parts[1]}",
               "--load-filaments", parts[2],
               "--slice", "0", "--outputdir", td, str(stl_path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"sliced": False, "slice_error": str(e)[:200]}
        gcodes = sorted(Path(td).glob("*.gcode"))
        if r.returncode != 0 or not gcodes:
            return {"sliced": False, "slice_error": (r.stderr or r.stdout)[-200:]}
        gcode = gcodes[0]
        out = {"sliced": True, "print_min": None, "filament_g": None}
        head = gcode.read_text(encoding="utf-8", errors="ignore")[:16000]
        t = re.search(r"(?:estimated printing time.*?=|total estimated time:|model printing time:)"
                      r"\s*(?:(\d+)h)?\s*(?:(\d+)m)?", head, re.I)
        if t:
            out["print_min"] = (int(t.group(1) or 0) * 60) + int(t.group(2) or 0)
        f = re.search(r"(?:filament used \[g\]\s*=|total filament weight \[g\]\s*:)\s*([\d.]+)", head, re.I)
        if f:
            out["filament_g"] = float(f.group(1))
        return out


def main() -> int:
    out_dir = Path(sys.argv[1]).resolve()
    do_slice = "--no-slice" not in sys.argv
    stls = sorted(out_dir.glob("*.stl"))
    if not stls:
        report = {"pass": False, "reason": "no_stl"}
    else:
        stl = stls[0]
        report = {"stl": stl.name, **mesh_stats(stl)}
        report.update(slice_stl(stl) if do_slice else {"sliced": None})
        fails = []
        if not report["watertight"]:
            fails.append("not_watertight")
        if report["bodies"] != 1:
            fails.append(f"bodies={report['bodies']}")
        if report["overhang_pct"] > OVERHANG_FAIL_PCT:
            fails.append(f"overhang={report['overhang_pct']}%")
        if report.get("sliced") is False:
            fails.append("slice_failed")
        if PRINT_MIN_MAX > 0 and report.get("print_min") and report["print_min"] > PRINT_MIN_MAX:
            fails.append(f"print_time={report['print_min']}min>max{PRINT_MIN_MAX}")
        fails += lint_code(out_dir)
        fc = out_dir / "fit_checks.py"
        if fc.is_file():
            r = subprocess.run([sys.executable, str(fc)], capture_output=True,
                               text=True, timeout=120, cwd=out_dir)
            report["fit_checks"] = "pass" if r.returncode == 0 else \
                (r.stdout + r.stderr)[-200:]
            if r.returncode != 0:
                fails.append("fit_check_failed")
        report["pass"] = not fails
        report["fails"] = fails
    (out_dir / "gate.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(("GATE PASS " if report["pass"] else "GATE FAIL ") + json.dumps(report))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
