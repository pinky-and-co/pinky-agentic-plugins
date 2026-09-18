---
name: aller
description: "Export selected kanban cards, their chantier context, a filtered _AGENT_LOG, a repo code subtree, and a rendered provider guide into one zip for an agent working outside Claude Code (Cortex/Snowsight, claude.ai web, ...). Moves the exported cards to 3-in-progress with an export marker. Use when the user says 'exporte ces cards pour <agent externe>', 'envoie ce travail à Cortex/Snowsight', or names specific card ids to hand off outside the session."
argument-hint: "<chantier-dir> --card <id> [--card <id> ...] --repo <path> --touched <file> [...]"
allowed-tools: mcp__plugin_pc-delegate_pc-delegate__export_subtree, Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/aller/scripts/build_aller.py:*), Read
---

# Aller — export a job for an agent working outside Claude Code

## Step 1 — Resolve the touched files

From the selected cards' own content (and, when relevant, `git status`/`git diff` on the target
repo), name the files the external agent actually needs to see or edit. This is a judgment call —
never guess silently; if it is unclear, ask.

## Step 2 — Compute the subtree

```
mcp__plugin_pc-delegate_pc-delegate__export_subtree(repo_path=<repo>, touched_files=[...])
```

Returns the touched files plus their direct imports (depth 1) — repo-relative paths, one per line.

## Step 3 — Build the export

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/aller/scripts/build_aller.py" \
  --chantier <chantier-dir> \
  --card <id> [--card <id> ...] \
  --repo <repo-path> \
  --repo-file <path> [--repo-file <path> ...] \
  --agent-log-dir <chantier-workspace>/_AGENT_LOG \
  --guide "${CLAUDE_PLUGIN_ROOT}/skills/aller/references/providers/cortex/GUIDE.md" \
  --out <output.zip>
```

Run with `--dry-run` first to preview which cards would move and how many files would be offered
— no write happens in that mode. The real run moves the selected cards to `3-in-progress` (an
`exported:` marker is set on each), applies the secrets denylist to every repo file and to
`_AGENT_LOG` entries, and writes the zip. Report any `🚫 excluded (secrets denylist)` line to the
user as-is — an exclusion is the filter doing its job, not a failure.

## Step 4 — Hand off

`build_aller.py` prints a ready-to-paste prompt for the external agent's chat, after the zip is
written — hand that to the user as-is alongside the zip path. Nothing beyond this is this skill's
job — sending the zip, and reintegrating what comes back (the `retour` skill, same plugin), are
separate, explicit steps.
