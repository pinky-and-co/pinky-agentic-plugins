---
name: init
description: Scaffold a fresh `_KANBAN/INDEX.md` + nested `_AGENT_LOG/` at a workspace root, and declare its additional board roots in `~/.claude/settings.pinky.json` (`kanban_boards`). Use before a board exists at all, or to migrate a workspace still declaring its extra boards through a `*.code-workspace`'s `folders[]` / `KANBAN_ROOTS`.
argument-hint: "[--workspace <path>]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/init/scripts/pc_kanban_init.py:*)
---

# init — scaffold a board, declare its extra roots

Every other `pc-kanban` skill assumes `_KANBAN/` already exists. This one creates it — the one time
a workspace has none yet, or is still declaring its additional boards the pre-`kanban_boards` way.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/init/scripts/pc_kanban_init.py" [--workspace <path>]
```

`--workspace` defaults to the current directory — generic, no repo-specific default baked in.

## What it does

1. Creates `_KANBAN/` at the resolved root if absent.
2. Nests `_AGENT_LOG/` under it — folding in a pre-existing sibling `_AGENT_LOG/` first
   (`pc_kanban_migrate_agent_log.py`), so a workspace never migrated ends up with its journal split
   across two directories.
3. Creates `INDEX.md` if absent: a minimal `## Board` table (legend + header, one dormant row per
   `CHANTIER{NNNN}-<slug>/` directory already on disk so an existing chantier is never rendered
   invisible by an empty table). Everything else a hand-maintained `INDEX.md` accretes over time —
   conventions, product topology — is this workspace's own prose, not scaffolded here.
4. Merges whatever extra board root was already visible for this workspace via the retired
   discovery mechanism (a `*.code-workspace`'s `folders[]`, or `KANBAN_ROOTS`) into
   `kanban_boards[<root>]` of `~/.claude/settings.pinky.json` — union with anything already
   declared there, `model_pool`/`keys` and every other top-level key read back untouched.

Idempotent throughout: a rerun on an already-scaffolded root changes nothing on disk. Never
scaffolds a first chantier — that is skill `grill`'s job.

## Declaring a board this skill did not migrate

Skill `kanban` (§ Orgs, `references/multi-board.md`) reads every additional board exclusively from
`kanban_boards[<primary-root>]` — there is no other declaration path. To hand-declare one without a
`*.code-workspace` to migrate from, edit `~/.claude/settings.pinky.json` directly: the key is the
primary root's resolved absolute path, the value a list of resolved absolute paths to each
additional board's own root (never the root's own path in its own list).
