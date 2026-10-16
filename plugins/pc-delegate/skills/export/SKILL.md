---
name: export
description: "Export selected kanban cards, their chantier context, a relevance-filtered _AGENT_LOG (exported-card slug mentions within the last 60 days, plus the secrets denylist), a repo code subtree, and a rendered guide into one zip for an agent working outside Claude Code. Moves the exported cards to 3-in-progress with an export marker. Use when the user asks to export cards or hand a job off to an external agent."
argument-hint: "<chantier-dir> --card <id> [--card <id> ...] --repo <path> --touched <file> [...]"
allowed-tools: mcp__plugin_pc-delegate_pc-delegate__export_subtree, Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/export/scripts/build_export.py:*), Read
---

# Export — export a job for an agent working outside Claude Code

## Step 1 — Resolve the touched files

From the selected cards' own content (and, when relevant, `git status`/`git diff` on the target
repo), name the files the external agent actually needs to see or edit — plus any hygiene hook or
test that targets one of them (a `repo: local` hook whose own script is a file in the repo and
whose `files:` pattern matches an edited file, the test module covering an edited one). A hook with
no file of its own (a third-party one like `trailing-whitespace`) adds nothing. This is the one list
passed to `export_subtree` below and later to `--repo-file`. It is a judgment call — never guess
silently; if it is unclear, ask.

## Step 2 — Compute the subtree

```
mcp__plugin_pc-delegate_pc-delegate__export_subtree(repo_path=<repo>, touched_files=[...])
```

Returns the touched files plus their direct imports (depth 1) — repo-relative paths, one per line.

## Step 3 — Build the export

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/export/scripts/build_export.py" \
  --chantier <chantier-dir> \
  --card <id> [--card <id> ...] \
  --repo <repo-path> \
  --repo-file <path> [--repo-file <path> ...] \
  --agent-log-dir <chantier-workspace>/_KANBAN/_AGENT_LOG \
  --guide "${CLAUDE_PLUGIN_ROOT}/skills/export/references/GUIDE.md" \
  --out <output.zip>
```

Run with `--dry-run` first to preview which cards would move and how many files would be offered
— no write happens in that mode. The real run moves the selected cards to `3-in-progress` (an
`exported:` marker is set on each), applies the secrets denylist to every repo file, and ships
only the `_AGENT_LOG` entries relevant to the exported cards: an entry must mention an exported
card's slug in its content (a chantier token alone never matches) and be dated within the last 60
days by its `YYYY-MM-DD_` filename prefix, on top of the secrets denylist. When no entry matches,
the zip ships without `_AGENT_LOG` and the script prints an explicit line saying so. Report any
`🚫` line (a secrets denylist exclusion, or the zero-match `_AGENT_LOG` notice) to the user as-is
— each is a filter doing its job, not a failure.

## Step 4 — Hand off

`build_export.py` prints a ready-to-paste prompt for the external agent's chat, after the zip is
written — hand that to the user as-is alongside the zip path. Sending the zip, and reintegrating what
comes back (the `reintegrate` skill, same plugin), are separate, explicit steps.
