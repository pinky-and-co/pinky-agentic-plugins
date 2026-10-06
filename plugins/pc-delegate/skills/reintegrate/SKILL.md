---
name: reintegrate
description: "Reintegrate the results an external agent (Cortex/Snowsight, claude.ai web, any agent working outside Claude Code) produced — a zip or directory of the files it created or modified. Applies safely (new files OK, a locally-diverged file is refused not overwritten), classifies each Python file comments-only vs code-changed, runs the target repo's own verify loop, then hands off to `commit` — never an automatic commit/push/PR. Use when the user says 'réintègre ce zip', 'intègre le retour de …', or hands you a directory of externally-produced files to bring back into a repo."
argument-hint: "<zip-or-directory> --repo <path> [--agent-log-dir <path>] [--card <id> ...]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/reintegrate/scripts/apply_reintegrate.py:*), Bash(git status:*), Bash(rtk git status:*), Bash(git diff:*), Bash(rtk git diff:*), Bash(ruff check:*), Bash(rtk ruff check:*), Bash(ruff format:*), Bash(rtk ruff format:*), Bash(mypy:*), Bash(rtk mypy:*), Bash(pytest:*), Bash(rtk pytest:*), Bash(prek run:*), Bash(pre-commit run:*), Bash(rtk pre-commit run:*), Read, Skill
---

# Reintegrate — reintegrate an external agent's results

## Step 1 — Apply

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/reintegrate/scripts/apply_reintegrate.py" <source> --repo <path> \
  --agent-log-dir <chantier-workspace>/_KANBAN/_AGENT_LOG --card <id>
```

`--agent-log-dir`/`--card` are optional — pass them whenever the reintegration belongs to a known
chantier/card, so a `REPORT.md` at the root of `<source>` (the external agent's own account of
what it did, any judgment call, anything found but out of scope) gets written as a new dated
`_AGENT_LOG` entry instead of only printed to the terminal. `REPORT.md` is never applied into the
repo — it is reserved, pulled out before the file-apply pass.

Run with `--dry-run` first when the source is unfamiliar or large — same output, nothing written.
The script's own report already separates what happened:

- **created** / **applied** — written, with a `.py` file tagged `comments-only` or `code-changed`.
- **traversal-refused** — a `..` or absolute entry, never written, always refused.
- **dirty-skipped** — a tracked file with a local uncommitted diff since the export.
- **conflict-skipped** — an untracked file already at that path, different content.

Report the skipped entries to the user as-is — a skip is not a failure to paper over, it is the
guard doing its job (a local edit that would otherwise be silently lost). If a `REPORT.md` printed
inline (no `--agent-log-dir` given) names something worth tracking, fold it into the card's own
handoff yourself — the script only persists it when told where.

## Step 2 — Verify loop

For every repo touched, run its own lint/type-check/test/pre-commit tooling (`ruff check --fix` +
`ruff format`, `mypy`, `pytest`, `prek run` or `pre-commit run` — whichever the target repo
declares). Two different reactions, never blur them:

- **Mechanical** (formatting, an unsorted import, a redundant `noqa`) — auto-apply without asking.
- **Anything touching logic or security** (a missing import that changes behaviour, a suppression
  marker, a file placed at the wrong path, a defaulted value that changes runtime behaviour) —
  **stop and ask**. Never auto-correct a judgment call.

A file classified `code-changed` in Step 1 is exactly the set to read closely here — a
`comments-only` file changed nothing the verify loop would ever flag.

## Step 3 — Hand off

Delegate to the `commit` skill (`pc-git`) for the actual commit. Never commit, push, or open a PR
from inside this skill — that stays an explicit, separate user-visible step, same discipline as
every other skill in this workspace.
