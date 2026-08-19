# pc-core

Generic workspace mechanics for Claude Code: gitmoji-convention git workflow (commit, commit +
push + PR/MR, branch cleanup), and permission guards (`careful`, `freeze`, `deny`, `guard`),
toggled through one `permission-change` skill.

## Overview

Being invoked is the confirmation: `commit` and `commit-and-pr` do not pause to propose and wait
before acting — an explicit invocation is already consent. The gitmoji emoji/type table lives in
`skills/{commit,commit-and-pr}/references/commit-convention.md`; both skills read it before
choosing an emoji, never relying on generic training-data knowledge of gitmoji.dev.

## Skills

### `commit`

Commits the current changes using the gitmoji convention.

**What it does:**

1. Identifies the target repo (standalone layout) or discovers dirty repos (workspace layout)
1. Runs `prek`/`pre-commit` before staging, if present
1. Reads the emoji/type convention table
1. Groups changes into one commit per logical group, trusting its own judgment on grouping and
   message wording
1. Commits — no pause, no proposal step

**Usage:** `/commit` or `/commit <repo-name>` or `/commit --all` (bulk mode: forks one child per
dirty repo)

**Features:**

- Silently resolves known noise files (`.DS_Store`, etc.) into `.gitignore` instead of asking
- Never proposes the `save` type — that's a human-only WIP savepoint action
- Runs on Haiku, forked (`context: fork`) — cheap and isolated by default

### `commit-and-pr`

Commit, push, and open a PR (GitHub) or MR (GitLab) — the full chain, auto end-to-end.

**What it does:**

1. Same target identification as `commit`
1. Detects the provider from `git remote get-url origin` (never guessed)
1. Reads the repo's `CLAUDE.md` for a `Branching:` line to resolve the PR/MR base branch
1. Creates a feature branch if currently on a permanent branch
1. Commits, pushes, and opens the PR/MR — checks for an existing open one first
1. Reports the PR/MR URL

**Usage:** `/commit-and-pr` or `/commit-and-pr <repo-name>` or `/commit-and-pr --all`

**Requirements:**

- `gh` (GitHub) or `glab` (GitLab), installed and authenticated, matching the detected provider
- A remote named `origin`

**Note:** `disable-model-invocation: true` — this skill only runs when explicitly invoked, never
triggered by Claude's own judgment (it commits, pushes, *and* opens a PR/MR in one uninterruptible
chain).

### `clean-gone`

Deletes local branches (and their worktrees) already deleted on the remote.

**What it does:**

1. Lists local branches, identifying `[gone]` status
1. Removes any associated worktrees first
1. Deletes the `[gone]` branches
1. Reports what was removed

**Usage:** `/clean-gone`

**Safe by construction:** only touches branches the remote has already dropped — never anything
with unpushed or unmerged work.

### `branch-workflow`

Reference knowledge, not directly invocable (`user-invocable: false`) — the naming conventions and
comparison table for the 6 branching strategies (Feature Branching, Git Flow, GitHub Flow, GitLab
Flow, Release Branching, Trunk-Based Development). `commit-and-pr` consults it to resolve a PR
base branch; a repo's own `CLAUDE.md` `Branching:` line is always authoritative over this table.

### `permission-change` — careful / freeze / deny / guard

One skill, one positional mode word — not four near-identical skills to remember. Enforced by
the shared `skills/permission-change/scripts/guard.py` PreToolUse hook, all four modes share one state file.

- **`careful`** — while on, a destructive `Bash` command (`rm -rf`, `git push --force`,
  `git reset --hard`, `DROP`, `TRUNCATE`, `DELETE FROM` without `WHERE`, `sudo`, …) returns
  `permissionDecision: ask` instead of running silently. A speed bump, not a block.
- **`freeze <dir>...`** — a whitelist: locks `Edit`/`Write`/`MultiEdit`/`NotebookEdit` to
  the given directories; anything outside is denied.
- **`deny <dir>...`** — the inverse of `freeze`: a blocklist. The given directories become
  unwritable; everything else stays open. Not the same as the `permissions.deny` settings key
  (harder guarantee, session-start only) — see the skill's own note.
- **`guard <dir>...`** — convenience: `careful` on + `freeze` to the given directories in
  one call.

**Honest limit, all four**: only the write-class TOOLS are covered. A Bash write (`sed -i`, `tee`,
`>`, `mv`/`cp`) is NOT intercepted — for a hard guarantee, use `permissions.deny` instead.

**Usage:** `/permission-change careful [on|off]`,
`/permission-change freeze <dir>... | off`,
`/permission-change deny <dir>... | off`,
`/permission-change guard <dir>... | on | off`

## Best Practices

- Let `commit` group changes on its own judgment — it writes better commit messages and groups
  changes more consistently than a rigid rule would.
- Use `commit` during routine development; switch to `commit-and-pr` once ready to open a PR/MR.
- Run `clean-gone` periodically, especially after merging several PRs.
- A repo without a `Branching:` line in its `CLAUDE.md` gets a base-branch resolution warning from
  `commit-and-pr` — add the line rather than letting it guess.

## Troubleshooting

**`commit` reports nothing to commit** — no staged or unstaged changes; run `git status` to check.

**`commit-and-pr` fails to open a PR/MR** — confirm `gh auth status` (GitHub) or `glab auth status`
(GitLab) succeeds, and that `origin` is set.

**Base branch looks wrong** — check the repo's `CLAUDE.md` for a `Branching:` line; without one,
`commit-and-pr` falls back to `git merge-base` and flags the missing declaration.
