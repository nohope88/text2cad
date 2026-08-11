# text2cad lessons (auto-maintained — repair sessions append here)
# A lesson that repeats must GRADUATE to code: a golden block, a gate linter
# check, or a brief-template constraint. Graduated lessons are marked [GRADUATED].

- Always fuse every piece into ONE solid with a single union chain, and make each piece OVERLAP its neighbors by >= 1mm — tangent/touching faces do NOT fuse and produce multiple bodies (Huggy 2026-08-10: 13 bodies, not watertight).
- Before exporting, verify in the cad tool JSON that the part is a single connected watertight solid (bodies == 1); never export on "no error" alone.
- [GRADUATED → gate bridge_span check] A wide flat internal ceiling (sealed-chamber roof) is an unsupported bridge that aggregate overhang% cannot see — what matters is local SPAN (inscribed-circle diameter of each down-facing patch), not area: a 16mm slot bridges fine, a 50mm chamber roof sags. Gate now fails span > 25mm (eclipse-v2 printability lens 2026-08-11; retro-caught eclipse-v1 at 52.7mm and validated robot at 23mm).
- [GRADUATED → BUILD prompt numeric-contract rule] Every explicit number the brief states must become an assert in params.py validate() — the build used a 6mm fillet where the brief demanded >=10mm twice (eclipse-v2 fidelity lens 2026-08-11).
- [GRADUATED → DRAFT prompt hero-legibility rule] A hero render a stranger cannot decode in 3 seconds fails sellability regardless of geometry quality — orient defining features toward the camera and make every key feature visible in at least one render (eclipse-v2 sellability lens 2026-08-11).
