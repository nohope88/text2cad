"""Golden blocks — frozen, verified CadQuery subparts (composition over invention).

Human-authored (SkillsBench 2026: human-authored skills +16.2pt vs ~0 for
LLM-authored). Every block must pass blocks/testbench.py before use; changes
re-run the testbench. Contracts in BLOCKS.md.

All dimensions in mm. Every function returns a cq.Workplane solid.
"""
import cadquery as cq

MIN_WALL = 1.6  # FDM floor enforced across all blocks


def rounded_box(w, d, h, corner_r=3.0, top_fillet=0.0, bottom_chamfer=0.6):
    """Box with corner rounds BAKED INTO THE 2D PROFILE, then extruded.

    This is the graduated Huggy lesson: a blanket .edges().fillet() makes OCCT
    build spherical vertex-blends that tessellate into phantom sliver bodies.
    Profile arcs + optional top-only fillet / bottom-only chamfer never build a
    vertex blend, so the result stays one watertight solid.
    """
    assert corner_r < min(w, d) / 2, "corner_r must be < half the smaller side"
    s = (cq.Workplane("XY").sketch()
         .rect(w, d).vertices().fillet(corner_r).finalize()
         .extrude(h))
    if top_fillet > 0:
        s = s.edges(">Z").fillet(top_fillet)
    if bottom_chamfer > 0:
        s = s.edges("<Z").chamfer(bottom_chamfer)
    return s


def hollow_box(w, d, h, wall=2.0, corner_r=3.0):
    """Rounded-profile OPEN-TOP container (tray/pot/holder). Always open:
    a sealed internal void is an FDM antipattern (trapped air, 2-surface mesh)
    — testbench-caught 2026-08-11. Need a lid? Model it as a second part."""
    assert wall >= MIN_WALL, f"wall must be >= {MIN_WALL}mm"
    s = rounded_box(w, d, h, corner_r, top_fillet=0.0, bottom_chamfer=0.0)
    return s.faces(">Z").shell(-wall)


def angled_stand(w=70, depth=80, height=75, base_t=4.0, nose_t=8.0, base_ext=0.0):
    """Solid right-wedge stand body (leaning face = hypotenuse) on a flat base.
    nose_t keeps a blunt top edge (printable, no knife edge). base_ext extends
    the base FORWARD past the slope foot — union a lip bar on it to form a
    device channel (the verified phone-stand recipe in BLOCKS.md)."""
    assert base_t >= 2.0 and nose_t >= 2.0
    pts = [(0, 0), (depth + base_ext, 0), (depth + base_ext, base_t)]
    if base_ext > 0:
        pts.append((depth, base_t))  # zero-length edge if appended when ext == 0
    pts += [(nose_t, height), (0, height)]
    return (cq.Workplane("XZ").polyline(pts).close()
            .extrude(w).translate((0, w / 2, 0)))


def phone_slot_cutter(w=85, phone_t=13.5, depth=14, angle_deg=27, clearance=1.5):
    """NEGATIVE volume: subtract from a body to make an angled phone groove.
    angle_deg tilts the slot back from vertical (0 = upright). Position with
    .translate() so the cutter's floor sits where the groove floor belongs."""
    t = phone_t + clearance
    cutter = (cq.Workplane("XY").rect(w, t).extrude(depth + t)
              .rotate((0, 0, 0), (1, 0, 0), -angle_deg))
    return cutter


def snap_hook(w=12.0, t=3.0, arm_l=25.0, hook_depth=6.0, hook_t=3.0):
    """Cantilever J-hook (wall hook / clip finger), flat profile extruded to w.
    Root at origin extending +X, hook lip turning up +Z at the far end."""
    assert t >= 2.0 and hook_t >= 2.0
    pts = [(0, 0), (arm_l, 0), (arm_l, hook_depth + hook_t),
           (arm_l - hook_t, hook_depth + hook_t), (arm_l - hook_t, hook_t),
           (0, t)]
    return (cq.Workplane("XZ").polyline(pts).close()
            .extrude(w).translate((0, w / 2, 0)))


BLOCKS = {
    "rounded_box": rounded_box,
    "hollow_box": hollow_box,
    "angled_stand": angled_stand,
    "phone_slot_cutter": phone_slot_cutter,
    "snap_hook": snap_hook,
}
