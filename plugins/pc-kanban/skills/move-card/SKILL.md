---
name: move-card
description: Move one kanban card by its slug — `select` (1-todo to 2-selected-for-dev, checked), `archive` (done card, requirements retired) or `drop` (obsolete card, with a reason). Typed with a conforming verb and slug it runs with no model call. Use when the user selects, archives or drops a card.
argument-hint: "select <card-slug> [--force] | archive <card-slug> | drop <card-slug> <reason...>"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py:*)
---

# move-card — select, archive or drop one card

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py" <select|archive|drop> <card-slug> [reason...]
```

`<card-slug>` is the card's filename without `.md`, looked up in every kanban board; zero or several
matches is reported, never guessed. Selection, archiving and dropping are the user's decisions: run
the verb only on their word, never on your own initiative.

| Verb | Looks in | Does |
|---|---|---|
| `select` | `1-todo/` | Moves to `2-selected-for-dev/` and sets `date:` — after checking that the card traces to at least one requirement declared in its chantier's story, has a valid `model`, a `## Done when`, and that any `blocked_by` card is done. Every unmet condition is listed. `--force` skips the requirements check only — a card that deserves no requirement of its own; the other conditions still apply. |
| `archive` | `4-done/`, `3-in-progress/` | `chantier`'s `archive_card.sh`: dated archive, requirements retired, reviewed-box check. A card carrying a durable decision needs its ADR first. |
| `drop` | `0-ideas/` to `3-in-progress/` | `chantier`'s `drop_card.sh`: `-dropped` archive, the reason appended, requirements retired. To keep the requirements, run `drop_card.sh --keep-requirements` instead. |

## When the script refuses

The direct command stays silent on any refusal and this skill runs the script again, so you see the
error. Take it from there instead of relaying it:

- **Unknown or ambiguous slug, unknown verb** — look for the closest card name or verb the user meant
  and ask which one; never pick one yourself.
- **`select` refused as incomplete** — propose the fix for each listed condition (a `model:` with the
  reason, the requirements to trace, a `## Done when`) and apply it only on the user's OK, then
  select again. A blocker that is not done, or was dropped, is the user's call.
