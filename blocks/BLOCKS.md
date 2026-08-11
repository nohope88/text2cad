# Golden Blocks — verified CadQuery subparts (v1, 2026-08-11)

Composition over invention: PREFER these frozen, testbench-verified blocks over
hand-building geometry. Import: `import sys; sys.path.insert(0, "<pipeline>/blocks");
from cadblocks import rounded_box, hollow_box, angled_stand, phone_slot_cutter, snap_hook`

Status: **APPROVED by Tam 2026-08-11** (after 3 review rounds: buried slot →
removed-volume assert; detached slot → phone-mock fit-check; missing cable
route → plug-mock fit-check). Human-curated per SkillsBench 2026 (+16.2pt vs ~0
for LLM-authored). Every change must re-run `testbench.py` (ALL PASS required)
and get re-approved.

| Block | Returns | Contract |
|---|---|---|
| `rounded_box(w,d,h,corner_r=3,top_fillet=0,bottom_chamfer=0.6)` | solid | Corner rounds baked into 2D profile (graduated Huggy lesson — NEVER blanket `.edges().fillet()`). corner_r < min(w,d)/2. |
| `hollow_box(w,d,h,wall=2,corner_r=3)` | solid | Open-top container, wall >= 1.6. Sealed voids forbidden (FDM antipattern, testbench-caught). Lid = separate part. |
| `angled_stand(w=70,depth=80,height=75,base_t=4,nose_t=8)` | solid | Right-wedge stand body on XZ profile; blunt nose (no knife edge). Cut slots from it; shell if volume budget demands. |
| `phone_slot_cutter(w=85,phone_t=13.5,depth=14,angle_deg=27,clearance=1.5)` | NEGATIVE volume | Subtract for an angled phone groove. Position floor with `.translate()`. Composition with angled_stand is testbench-verified. |
| `snap_hook(w=12,t=3,arm_l=25,hook_depth=6,hook_t=3)` | solid | Cantilever J-hook profile extruded to w; min thickness 2mm. |

Print rules all blocks inherit: min wall 1.6mm, printable base-down without
supports, one watertight solid per part.

## Composition rules (learned the hard way — 2 review rounds with Tam)
- A cutter tilted PARALLEL to the face it should pierce stays buried at constant
  depth and never opens. Verify removed-volume fraction after every cut
  (≈100% = buried void, ≈0% = missed).
- A slot standing APART from the leaning face is not a stand — the device must
  physically REST ON the slope. Function must be fit-checked with a device mock
  (testbench does: 8×70×150 phone box leaned at slope angle, clash < 100mm³),
  never eyeballed from "the mesh is valid".
- **THE phone-stand recipe (verified):** steep `angled_stand`
  (lean = atan((depth−nose_t)/(height−base_t)) ≈ 25-30°, e.g. depth=45 h=80)
  with `base_ext = channel + lip_t`, union a `rounded_box(lip_t, w, lip_h)` bar
  at x = depth + channel: device leans on the slope, foot sits in the channel,
  lip retains it. See testbench.py composition case.
- **Charge-while-docked is mandatory for dock/stand parts** (Tam review
  2026-08-11): cut a centered cable notch (~20mm wide) through lip + channel
  floor + slope foot, open to the bottom. Testbench asserts a 12×12mm USB plug
  mock passes under the device port with ~0 clash.
