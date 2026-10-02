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

## Three rules

- **No session-narrated prose.** No `## Decisions (grill YYYY-MM-DD)` block, no `## State` dump.
  When a grill closes, its outcome folds into `## Requirements` and `## Open questions`.
- **A delivered requirement leaves the table when its card leaves the board** — mechanically:
  `archive_card.sh`/`drop_card.sh` call `retire_requirements.py`, which removes the rows the
  card's frontmatter `requirements` lists — unless another open card still carries the id — and
  copies them verbatim into the archived card (the board is not versioned; that copy is the only
  way back). `drop_card.sh --keep-requirements` keeps the story when the need survives the card.
  The durable form of a delivered requirement is the code, never this table.
- **A retired requirement is deleted**, never struck through; its id stays burnt.

A story past ~200 lines has accreted prose — prune it back to the template, don't grow the file.
