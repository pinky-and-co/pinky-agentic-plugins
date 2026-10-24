---
name: reintegrate
description: "Reintegrate what an external agent (Cortex/Snowsight, claude.ai web, any agent outside Claude Code) produced — a zip or directory of created or modified files: applied safely (a locally-diverged file is refused, never overwritten), Python files classified comments-only vs code-changed, the repo's own verify loop run, the diff reviewed, the PR opened through `commit-and-pr`, covered cards archived. Use when the user says 'reintegrate this zip', 'integrate the return of …', or hands over a directory of externally produced files."
argument-hint: "<zip-or-directory> --repo <path> [--agent-log-dir <path>] [--card <id> ...]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/reintegrate/scripts/apply_reintegrate.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/reintegrate/scripts/close_cards.py:*), Bash(git status:*), Bash(git -C * status*), Bash(rtk git -C * status*), Bash(rtk git status:*), Bash(git diff:*), Bash(git -C * diff*), Bash(rtk git -C * diff*), Bash(rtk git diff:*), Bash(ruff check:*), Bash(rtk ruff check:*), Bash(ruff format:*), Bash(rtk ruff format:*), Bash(mypy:*), Bash(rtk mypy:*), Bash(pytest:*), Bash(rtk pytest:*), Bash(prek run:*), Bash(pre-commit run:*), Bash(rtk pre-commit run:*), Read, Skill, Agent
---

# Reintegrate — reintegrate an external agent's results

## Step 1 — Apply

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/reintegrate/scripts/apply_reintegrate.py" <source> --repo <path> \
  --agent-log-dir <chantier-workspace>/_KANBAN/_AGENT_LOG --card <id>
```

`--agent-log-dir`/`--card` are optional — pass them whenever the reintegration belongs to a known
chantier/card, so a `REPORT.md` at the root of `<source>` (the external agent's own account) gets
written as a new dated `_AGENT_LOG` entry instead of only printed. `REPORT.md` is never applied into
the repo — it is pulled out before the file-apply pass.

Run with `--dry-run` first when the source is unfamiliar or large — same output, nothing written.
The script's own report already separates what happened:

- **created** / **applied** — written, with a `.py` file tagged `comments-only` or `code-changed`.
- **traversal-refused** — a `..` or absolute entry, never written, always refused.
- **dirty-skipped** — a tracked file with a local uncommitted diff since the export.
- **conflict-skipped** — an untracked file already at that path, different content.

Report the skipped entries to the user as-is — a skip is the guard doing its job, not a failure to
paper over. If a `REPORT.md` printed inline (no `--agent-log-dir` given) names something worth
tracking, fold it into the card's own handoff yourself.

## Step 2 — Verify loop

For every repo touched, run its own lint/type-check/test/pre-commit tooling (`ruff check --fix` +
`ruff format`, `mypy`, `pytest`, `prek run` or `pre-commit run` — whichever the target repo
declares). Two different reactions, never blur them:

- **Mechanical** (formatting, an unsorted import, a redundant `noqa`) — auto-apply without asking.
- **Anything touching logic or security** (a missing import that changes behaviour, a suppression
  marker, a file placed at the wrong path, a defaulted value that changes runtime behaviour) —
  **stop and ask**. Never auto-correct a judgment call.

Read the `code-changed` files of Step 1 closely here; a `comments-only` file changes nothing the
verify loop would flag.

## Step 3 — Review, ship, close

**Covered cards.** The return carries `EXPORTED_CARDS.txt` at its root (zip or directory), one card
slug per line — it is never applied into the repo.

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/reintegrate/scripts/close_cards.py" covered <source> \
  --chantier <chantier-dir> --repo-slug <org/repo>
```

Exit 0 prints the covered slugs. Exit 3 means the file is absent: the output lists this repo's
`exported:` cards in `3-in-progress` — ask the user which of them the return covers.

**Review.** Once the verify loop is green, call the `adversarial` MCP tool (`pc-models`, opt-in) on the diff — `base` the branch it forks from,
`tools: ["read", "grep"]`, `task` the covered cards' `## Why` and `## Done when`, as
`finishing-a-card.md` § "Reviewing before declaring a card done" details; `Agent(reviewer)` only
when the tool is absent from the session or answers `BLOCKED` or `ERROR`. Skip it only when every change is mechanical (formatting, import order, comments-only files). A KO verdict:
fix, then review again — at most 2 re-reviews, then stop and report to the user. When the review is
OK, check the `reviewed (<method>)` box of each covered card; when the review was skipped as
mechanical, check it as `reviewed (n/a — mechanical)`.

**Ship.** Run `commit-and-pr` (pc-git), never before the box is checked.

**Close.** After the PR exists, record the repo on each covered card and read the verdict:

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/reintegrate/scripts/close_cards.py" mark \
  --chantier <chantier-dir> --repo-slug <org/repo> --card <slug> [--card <slug> ...]
```

`mark` writes `reintegrated: [org/repo]` in the card's frontmatter. For each `archive <slug>` line,
run `move-card archive <slug>` (pc-kanban). A `keep <slug> missing: ...` line means other repos of a
multi-repo card have not returned: the card stays in `3-in-progress`, and the report names the
missing repos. A `skip <slug>: <reason>` line means the card was left untouched (unsafe slug, not in
`3-in-progress`, no inline `repos:` list, or the repo is not one the card lists): report it, never
archive it.
