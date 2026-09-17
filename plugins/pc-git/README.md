# pc-git

Gitmoji commit workflow, branch strategy, and branch-policy enforcement for Claude Code.

## Overview

Everything here shares one idea: a git action against N repos is dispatched work, not N
hand-written prompts. `commit` and `commit-and-pr` run a deterministic script
(`bulk_commit.sh`) that scans, stages, and groups repos by diff fingerprint — the model writes
one message per group, not one per repo. `clean-gone` follows the same shape for deleting
branches the remote already dropped.

A `PreToolUse` hook (`branch_guard.py`) blocks a guarded write on a permanent branch regardless
of which skill or agent triggered it — enforcement lives in the hook, not in each skill's prompt.

## Skills

- **`commit`** — commit the current changes using the gitmoji convention. `--all` commits every
  dirty repo in one deterministic pass.
- **`commit-and-pr`** — commit, push, and open a PR (GitHub) or MR (GitLab), end-to-end. `--all`
  handles every dirty repo in one pass; `--no-watch-ci` skips waiting on CI.
- **`clean-gone`** — delete local branches marked `[gone]` (remote already deleted them, e.g.
  after a merge) and their worktrees. Never touches unmerged work. `--all` sweeps every repo
  under the workspace root.
- **`branch-workflow`** *(reference)* — branch naming conventions and the 6 branching strategies
  (Feature Branching, Git Flow, GitHub Flow, GitLab Flow, Release Branching, Trunk-Based
  Development), and what each implies for where a branch bases from and merges to.
- **`commit-convention`** *(reference)* — the gitmoji + Conventional-Commits message format
  shared by `commit` and `commit-and-pr`.

## Agent

**`git-workflow`** — the dedicated-agent counterpart to `commit`/`commit-and-pr`/`clean-gone`,
for a reliable first action on a cold invocation instead of routing through the main session.

## Requirements

`git`, `gh` (GitHub) or `glab` (GitLab) on `PATH` for the PR/MR-opening skills.

## Dependencies

`pc-core`.
