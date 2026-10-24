# pc-git

Gitmoji commit workflow, branch strategy, and branch-policy enforcement.

## Overview

Everything here shares one idea: a git action against N repos is dispatched work, not N
hand-written prompts. `commit` and `commit-and-pr` run a deterministic script
(`bulk_commit.sh`) that scans, stages, and groups repos by diff fingerprint — the model
writes one message per group, not one per repo. `clean-gone` follows the same shape for
deleting branches the remote already dropped. Enforcement lives in hooks, not in each
skill's prompt: a `PreToolUse` hook blocks a guarded write on a permanent branch, another
refuses a branch or PR/MR that overlaps files already in flight, and a `SessionStart`
hook starts every session on a clean, up-to-date base.

## Skills

### `branch-workflow` *(reference)*

Branch naming conventions and the 6 branching strategies (Feature Branching, Git Flow,
GitHub Flow, GitLab Flow, Release Branching, Trunk-Based Development) — what each implies
for where a feature branch bases from and merges to. The generic reference behind the
guard applied before any `git add`/`git commit`; a repo's actual strategy is declared in
its own `.claude/settings.pinky.json` (`branch_workflow` key).

### `clean-gone`

Delete local git branches marked `[gone]` (deleted on the remote, e.g. after a merge) and
their worktrees. Auto — nothing here is destructive to unmerged work, `[gone]` means the
remote already dropped it. `--all` sweeps every repo under the workspace root in one pass,
symmetric with `commit-and-pr --all`.

```
/pc-git:clean-gone --all
```

### `commit`

Commit the current changes using the gitmoji convention. Bulk mode (`--all`) commits every
dirty repo in one deterministic pass.

```
/pc-git:commit --all
```

### `commit-and-pr`

Commit, push, and open a PR (GitHub) or MR (GitLab) — full flow, auto end-to-end. Bulk
mode (`--all`) handles every dirty repo in one deterministic pass. CI watching on each
opened PR/MR is the default; `--no-watch-ci` opts out.

```
/pc-git:commit-and-pr --all
```

### `commit-convention` *(reference)*

Reference knowledge for the gitmoji + Conventional-Commits-based commit message format
shared by the `commit` and `commit-and-pr` skills — read before writing any commit
message.

## Hooks

- **`branch_guard.py`** (`PreToolUse`, `Edit|Write|MultiEdit|NotebookEdit`) — hard-blocks
  a guarded write on a permanent branch: denies when the current branch is one of the
  declared strategy's permanent branches and the target path is not in the non-guarded
  list. Undeclared strategy, non-git target, or an already-checked-out topic branch
  degrade to nothing to enforce.
- **`pr_overlap_guard.py`** (`PreToolUse`, `Bash`) — guards against overlapping PRs: on
  `git switch -c` / `git checkout -b` / `gh pr create` / `glab mr create`, refuses and
  names the PR/branch already touching the same files. Lockfiles are excluded; never
  blocks on a tool error.
- **`clean_gone_hook.py`** (`SessionStart`) — starts every session on a clean,
  up-to-date base: fast-forwards the base branch (checked out or not), removes `[gone]`
  branches and their worktrees, and moves HEAD to the base only when HEAD itself was
  `[gone]`. Runs on the session's own repo only; nothing done means no output.
- **`pc_git_direct_command_runner.py`** (`UserPromptExpansion`, `/pc-git:clean-gone`) —
  runs the deterministic `clean-gone` script directly, with no model call, when the slash
  command is typed with conforming arguments. Always fails toward the normal skill run.

## Requirements

Python 3.12 or newer, resolved as `python3` on the `PATH`, for the hooks and skill
scripts (stdlib only). `bash` for the deterministic scripts (`bulk_commit.sh`,
`bulk_clean_gone.sh`). `git` throughout. `gh` (GitHub) or `glab` (GitLab) on `PATH` for
the PR/MR-opening skill and its CI watch.

## Dependencies

`pc-core`.
