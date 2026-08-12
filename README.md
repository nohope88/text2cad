# text2cad

Autonomous trend→product pipeline: scans X/HN trends, picks a 3D-printable
product people would impulse-buy, and produces print-ready `.step`/`.stl` —
fully automatic, one cycle per day.

```
 INPUT ────────────────────────────────────────────────────────────────
   X/HN trends (second-brain MCP)          or a direct prompt
        │                                        │
        ▼                                        │
 ┌─────────────────┐   discover_lessons.md      │
 │ ① DISCOVER      │◀──(taste learned from      │
 │ 5 ideas → score │    human rejects)          │
 │ top-3 → WINNER  │                            │
 └────────┬────────┘                            │
          ▼                                     ▼
 ┌──────────────────────────────────────────────────┐
 │ ② BRIEF                                          │
 │ 🔍 web research → must-have checklist            │
 │ spec in mm + ## Interfaces (device, cable, hand) │
 └────────┬─────────────────────────────────────────┘
          ▼
 ┌─────────────────┐
 │ ③ DRAFT CAD     │──▶ hero.png + multi-view ──▶ 📱 Telegram (FYI;
 └────────┬────────┘    saved to reference/         --auto skips waiting)
          ▼
 ┌──────────────────────────────────────────────────┐   ┌─────────────┐
 │ ④ BUILD                                          │◀──│ blocks/     │
 │ compose from GOLDEN BLOCKS (human-approved)      │   │ testbench-  │
 │ visual contract: match reference/ renders        │   │ verified    │
 │ writes fit_checks.py from Interfaces             │   └─────────────┘
 └────────┬─────────────────────────────────────────┘
          ▼                                  lessons.md
 ┌──────────────────────────────────┐   (hard rules, injected
 │ ⑤ GATE  (no LLM)                 │    into every prompt)
 │ trimesh: watertight? 1 body?     │        ▲
 │ OrcaSlicer real slice + print est│        │ repeated lesson MUST
 │ lesson-linter on code            │        │ graduate to code
 │ runs fit_checks.py               │        │
 └────────┬─────────────────────────┘        │
          ▼                                  │
 ┌──────────────────────────────────┐        │
 │ ⑥ PANEL — 3 independent lenses   │        │
 │ printability │ fidelity │ sell   │        │
 └────────┬─────────────────────────┘        │
          ▼                                  │
 ┌──────────────────────────────────┐        │
 │ ⑦ REPAIR (severity-tiered)       │────────┘
 │ broken 2 / functional 3 / cosm 2 │──▶ budget exhausted
 │ re-gate + rescore failed lenses  │    → 📱 ESCALATE
 └────────┬─────────────────────────┘
          ▼ all green
 OUTPUT ──────────────────────────────────────────────────────────────
   out/<slug>/  part.step + part.stl + renders + review notes
                + run.json (cost/turns per phase)
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

## Models

Per-phase model via `{PHASE}_MODEL` in `.env` — e.g. `BUILD_MODEL`,
`LENS-FIDELITY_MODEL` (phase name uppercased). Default: `claude-sonnet-5`
direct; if the `PANDA_DEV_KEY` proxy is enabled, values must use the proxy's
`anthropic,claude-opus-5` format instead.

Current mix (since 2026-08-12) — Opus 5 where mistakes cascade, Sonnet 5
default elsewhere:

| Phase | Model | Why |
|---|---|---|
| discover, brief, build | Opus 5 | spec/code errors cascade downstream |
| arbitration | Opus 5 | judgment call, no retry |
| repair2, repair3 | Opus 5 | escalation ladder above Sonnet repair1 |
| lens-fidelity | Opus 5 | on Sonnet it chronically exhausted 35 turns (2 runs, 2 timeouts) |
| draft, other lenses, repair1 | Sonnet 5 (default) | cheap, re-gated / rescored anyway |

Pre-mix `.env` kept at `.env.bak-pre-modelmix`.

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
