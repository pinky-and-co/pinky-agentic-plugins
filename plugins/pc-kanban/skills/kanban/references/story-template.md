# The chantier story — fixed template

Referenced from `SKILL.md`. Read before writing or pruning a story.

The story file is re-read in full on every orientation (the orchestrator's board report, every
spawned worker). Every line pays rent on a repeat read, so the structure is fixed and closed:

```
# Chantier NNNN — <name>

## Objective                 — one line + the constraint that shapes the rest
## Scope                     — in / out, dated
```

Section headers are English even when the statements are not — they are structural markers, not
prose.

## Where the rest lives

Requirements, open questions and steps live in **the card that carries them** (`SKILL.md` § Card
body), never in the story: an item in the story has no card to leave with, so nothing ever retires
it. A card leaving the board takes its items with it — archived, they stay readable in the card,
the only way back on an unversioned board. An item no card carries becomes a card (`0-ideas` if not
scoped yet).

The chantier-wide view — every requirement of the open cards, their open questions, the open cards
with blockers first, any id cited by an open card but defined in none, and any id defined with
different rows in several cards — is rebuilt on demand:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py" <chantier-dir>
python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py" --next-id <FAM> <chantier-dir>
```

`--next-id` counts open cards, the chantier's archive and the story, so a burnt id is never reused.
A story still carrying a `## Requirements` table is read as one more definer — by the view and by
`move-card select` — until its rows are moved into their cards.

## Two rules

- **No session-narrated prose.** No `## Decisions (grill YYYY-MM-DD)` block, no `## State` dump. A
  decision that shapes the whole chantier folds into `## Scope`, phrased for the present.
- **A card leaving the board while another open card still cites one of its ids** — archived or
  dropped — first hands those rows to an open card; otherwise they leave with it and `story_view.py`
  flags the citation as defined nowhere.

A story past ~50 lines has accreted card content — move it into its card, don't grow the file.
