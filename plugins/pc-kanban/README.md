# pc-kanban

File-based kanban board mechanics for Claude Code — chantiers, cards, permission guards, and
requirements interviews.

## Overview

The board lives as plain files under `_KANBAN/` — no external ticketing tool, no server. A
chantier (epic) is a directory of cards moving through columns; the agent maintains the board as
part of the work instead of waiting to be told "create a card" or "move it to done".

Two permission guards ship alongside the board mechanics because a mounted board needs writes an
ordinary repo-scoped session shouldn't otherwise have — see the `permission-change` skill and
`hooks/orchestrator_freeze.py`.

## Skills

- **`brainstorm`** — diverge before deciding: explore an idea, name its concepts, challenge generic
  claims, and hand back a brief with epistemic status, "Open items" and ordered steps. Written
  to run on claude.ai too; see [Uploading brainstorm to claude.ai](#uploading-brainstorm-to-claudeai).
- **`chantier`** — resume a chantier: load its story, cards, and in-progress handoff, report
  state, and take the next card.
- **`grill`** — interview a plan, idea, or imported brief until nothing is silently assumed, then
  write it into the chantier's cards — numbered, verifiable requirements, open questions and steps.
- **`kanban`** *(reference)* — the board's own mechanics: column lifecycle, card frontmatter
  schema, filiation, cross-agent log.
- **`permission-change`** — toggle the PreToolUse permission guards: `careful` (confirm
  destructive Bash), `freeze` (lock edits to a scope), `deny` (block edits to a scope), or `guard`
  (`careful` + `freeze` together).
- **`spawn-workers`** — from an orchestrator session, hand a selected card to one worker session
  per repo it touches.
- **`yt`** — summarize a YouTube video and verdict its relevance against the active chantier
  board.

### Uploading brainstorm to claude.ai

The zip is generated on demand and never committed:

```
python3 plugins/pc-kanban/skills/brainstorm/scripts/build_zip.py
```

It prints the path of `brainstorm-skill.zip` in the system temporary directory (`--out <path>`
picks another location, outside the repository). On claude.ai, open Settings, Capabilities, Skills,
upload the zip, and enable the skill. Run a brainstorm there, save the brief as a file, then run
`pc-kanban:grill <path-to-brief.md>` in Claude Code.

## Hooks

- **`stop_handoff.py`** — resolves the boards and agent-log directories mounted on this machine;
  other plugins' guards (see `pc-kanban:permission-change`'s board-dir carve-out) shell out to it
  rather than re-deriving board discovery.
- **`agent_log_writer.py`** — on SessionEnd, when the session committed or touched the board but
  wrote nothing under `_AGENT_LOG/`, hands the entry to a detached headless writer (haiku) fed a
  scrubbed transcript excerpt; the writer may decide nothing is worth logging.
- **`orchestrator_freeze.py`** — freezes an orchestrator session out of the repos it dispatches to.
- **`pc_kanban_secret_patterns.py`** — secret-pattern definitions shared by this plugin's guards.

## Requirements

Python 3.12 or newer, resolved as `python3` on the `PATH`, for the hooks and skill scripts.

## Dependencies

`pc-core`.
