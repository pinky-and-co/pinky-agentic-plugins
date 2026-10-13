---
name: move-card
description: Move one kanban card by its slug — `select` (1-todo to 2-selected-for-dev, checked), `archive` (done card, dated archive) or `drop` (obsolete card, with a reason). Typed with a conforming verb and slug it runs with no model call. Use when the user selects, archives or drops a card.
argument-hint: "select <card-slug|id/slug> [--force] | archive <card-slug|id/slug> | drop <card-slug|id/slug> <reason...>"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py:*)
---

# move-card — select, archive or drop one card

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py" <select|archive|drop> <card-slug|id/slug> [reason...]
```

`<card-slug>` is the card's filename without `.md`, looked up in every kanban board; zero or several
matches is reported, never guessed. When the same slug exists in two chantiers, name the card as
`<id>/<slug>` — the chantier's four-digit id and the slug, e.g. `0025/points-ouverts-sans-card`. Selection, archiving and dropping are the user's decisions: run
the verb only on their word, never on your own initiative.

| Verb | Looks in | Does |
|---|---|---|
| `select` | `1-todo/` | Moves to `2-selected-for-dev/` and sets `date:` — after checking that the card traces to at least one requirement defined in an open card's `## Requirements` of its chantier, has a valid `model` (and a valid `effort`, when set), a `## Done when`, and that any `blocked_by` card is done. Every unmet condition is listed. `--force` skips the requirements check only — a card that deserves no requirement of its own; the other conditions still apply. |
| `archive` | `4-done/`, `3-in-progress/` | dated archive, reviewed-box check, then the closing sequence (open cards, next-card candidates, session recommendation) — the card's requirements leave with it: move a row another open card still cites into that card first. A card carrying a durable decision needs its ADR first. |
| `drop` | `0-ideas/` to `3-in-progress/` | `-dropped` archive, the reason appended — the card's requirements leave with it. When the need survives the card, move its `## Requirements` rows into the card taking the need over first. |

## After `select`

A `select` run through this skill is a card taken now: once it succeeds, invoke
`pc-kanban:chantier <id> <slug>` yourself with the Skill tool (in place when it is already loaded) —
never ask the user to type the command. Selecting without taking is the typed command or the mod,
never this skill.

## When the script refuses

The direct command stays silent on any refusal and this skill runs the script again, so you see the
error. Take it from there instead of relaying it:

- **Unknown or ambiguous slug, unknown verb** — look for the closest card name or verb the user meant
  and ask which one; never pick one yourself. An ambiguous slug is refused with the `<id>/<slug>`
  form of each match: ask which one, then run the verb with that form.
- **`select` refused as incomplete** — propose the fix for each listed condition (a `model:` with the
  reason, the requirements to trace, a `## Done when`) and apply it only on the user's OK, then
  select again. A blocker that is not done, or was dropped, is the user's call.
