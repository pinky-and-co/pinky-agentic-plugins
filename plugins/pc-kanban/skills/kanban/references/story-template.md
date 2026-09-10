# The chantier story — fixed template

Referenced from `SKILL.md`. Read before writing or pruning a story.

The story file is re-read in full on every orientation (the orchestrator's board report, every
spawned worker loading its card's requirements). Every line pays rent on a repeat read, so the
structure is fixed and closed:

```
# Chantier NNNN — <name>

## Objective                 — one line + the constraint that shapes the rest
## Scope                     — in / out, dated
## Requirements              — the table: id · priority · statement · verification criterion
## Open questions            — what is knowingly undecided
## Implementation priority   — ordered list of what must exist first
```

Section headers are English even when the statements are not — they are structural markers, not
prose. `## Requirements` follows [requirements.md](requirements.md) — id format, the five families,
the four invariants, the table's columns.

## Two rules

- **No session-narrated prose.** No `## Decisions (grill YYYY-MM-DD)` block, no `## State` dump.
  When a grill closes, its outcome folds into `## Requirements` and `## Open questions` — a
  delivered decision's substance is already the verification criterion of its row; anything else
  worth keeping is an ADR, not a dated paragraph here.
- **No struck-through rows.** A requirement dropped once its card shipped leaves the table. Git
  holds why it went.

A story past ~200 lines has accreted prose — prune it back to the template, don't grow the file.
