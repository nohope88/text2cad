# text2cad

Autonomous trend→product pipeline: scans X/HN trends, picks a 3D-printable
product people would impulse-buy, and produces print-ready `.step`/`.stl` —
fully automatic, one cycle per day.

```
INPUT: trend X/HN (second-brain MCP)  |  or a direct prompt
  ① DISCOVER   5 ideas → scored → top-3 → winner + reason
  ② BRIEF      web research (must-have checklist) + spec mm + Interfaces
  ③ DRAFT      draft CAD → hero + multi-view renders (Telegram FYI)
  ④ BUILD      compose from approved GOLDEN BLOCKS + visual contract
               + auto-generated fit_checks.py from Interfaces
  ⑤ GATE       trimesh + OrcaSlicer slice + lesson-linter + fit-checks
  ⑥ PANEL      3 independent lenses: printability / fidelity / sellability
  ⑦ REPAIR     severity-tiered (broken 2 / functional 3 / cosmetic 2),
               exhausted → Telegram escalation
OUTPUT: out/<slug>/  .step + .stl + renders + review notes + run.json ($)
```

## Loops & self-improvement

- **In-run:** build loop (cad tool JSON), repair loop (tiered), panel rescore
  (failed lenses only).
- **Across runs:** every repair appends a lesson; a lesson that repeats MUST
  graduate to code (gate linter / golden block / brief constraint) — never
  advisory text twice. Human rejects teach `discover_lessons.md`.
- **Weekly:** `improve.py` analyzes the week's runs and improves the pipeline.
  Doc-tier changes (lessons) merge to main; **code-tier changes go to a PR**
  for human review. `blocks/` is human-approved by policy (SkillsBench 2026:
  human-curated skills +16.2pt vs ~0 LLM-authored).

## Ops (panda, UTC)

| Cron | What |
|---|---|
| `15 0 * * *` | `autoloop.py` — daily cycle, lessons auto-commit, Telegram summary |
| `0 1 * * 0` | `improve.py` — weekly self-improvement session |
| `0 4 * * *` | `watchdog.sh` — dead-man alert if heartbeat >28h stale |

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python cadquery trimesh numpy manifold3d
cp .env.example .env   # fill Telegram creds; OrcaSlicer paths for slice gate
./text2cad "phone stand with cable slot"      # one-shot
./text2cad --discover                          # proposal only
./text2cad --discover --auto                   # full cycle
```

Requires: `claude` CLI (authenticated), `uv`, OrcaSlicer (optional, for the
slice gate), the `cadcode` + `shape-analysis` skills in `~/.claude/skills/`,
and the `second-brain` MCP registered (trend source).
