---
name: init
description: Scaffold a fresh `_KANBAN/INDEX.md` + nested `_AGENT_LOG/` at a workspace root, declare it in `~/.claude/settings.pinky.json` (`kanban_boards`), and grant it native access in `~/.claude/settings.json` (`permissions.additionalDirectories`). Use before a board exists at all, or to grant access to one already declared by hand.
argument-hint: "[--workspace <path>]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/init/scripts/pc_kanban_init.py:*)
---

# init — scaffold a board, declare it, grant it

Every other `pc-kanban` skill assumes `_KANBAN/` already exists. This one creates it — the one time
a workspace has none yet.

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
4. Ensures `kanban_boards[<root>]` exists in `~/.claude/settings.pinky.json` — an empty list on a
   fresh root, left untouched if already present. `model_pool`/`keys` and every other top-level key
   are read back untouched.
5. Grants native access to `<root>/_KANBAN` and every board already listed in `kanban_boards[<root>]`
   — `permissions.additionalDirectories` in `~/.claude/settings.json`, union only, every other
   top-level key (`hooks`, `enabledPlugins`, `model`, …) read back untouched.

Idempotent throughout: a rerun on an already-scaffolded root changes nothing beyond granting a
board declared by hand since the last run. Never scaffolds a first chantier — that is skill
`grill`'s job. Reads nothing outside `_KANBAN`'s own tree and the two settings files above — no
`*.code-workspace`, no environment variable.

## Declaring an additional board

Skill `kanban` (§ Orgs, `references/multi-board.md`) reads every additional board exclusively from
`kanban_boards[<primary-root>]` — there is no other declaration path. Edit
`~/.claude/settings.pinky.json` directly, the same convention `model_pool`/`keys` already use in
that file: the key is the primary root's resolved absolute path, the value a list of resolved
absolute paths to each additional board's own root (never the root's own path in its own list).
Then rerun `init` on the primary root — it grants the newly-declared board in
`additionalDirectories` on its own.
