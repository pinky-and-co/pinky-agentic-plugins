---
name: commit
description: Commit the current changes using the gitmoji convention. Bulk mode (--all) commits every dirty repo in one deterministic pass. Use when the user asks to commit changes, or to commit every dirty repo in one pass.
argument-hint: "<repository-to-commit | /absolute/repo/path> or --all"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(test -e:*), Bash(find:*), Bash(rg:*), Bash(git rev-parse:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git checkout:*), Bash(git add:*), Bash(git commit:*), Bash(command -v prek:*), Bash(command -v pre-commit:*), Bash(prek run:*), Bash(pre-commit run:*), Bash(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/commit_trailer.py:*), Read, Write
---

# Commit — gitmoji convention

Being invoked is the confirmation. Analyse the diff and commit — do not propose and wait, just do
it. (A different trigger — the agent noticing a dirty tree unprompted, end of session — proposes
first per the workspace's commit-proactivity policy; that path never reaches this skill body until
consent already exists.)

## Step 1 — Identify the target(s)

`args` is whatever trails an `ARGUMENTS: ...` line at the very end of this prompt. **No such line
at all means no argument was passed** — that is the normal, expected shape for a bare invocation,
not missing information. Go straight to step 4 below; never ask for one instead.

The target passed to Step 2 is **always an absolute worktree root**, never a bare name. Resolve it
in this order:

1. **An absolute repo path** (starts with `/`) → checked first. Combined with `--all`, or with any
   other repo name or path, stop with a plain error before any tool call — a path is never mixed
   with another target. Otherwise the path must be exactly a repo's own directory: run
   `test -e "<path>/.git"` (quote the path). If it fails — a subdirectory of a repo, or a directory
   that merely contains repos, is not a repo path — say so plainly and stop, never guess another
   repo. Then that path is the target: go to Step 2 with it and never run the bulk script.
2. **`--all`, or an argument naming several repos** → the bulk branch below.
3. **An argument naming one repo** → resolve the name to a path via the same sweep the bulk branch
  uses (below): the candidate whose basename matches the argument. No match → say so, stop. Then go
  to Step 2 with that path.
4. **No argument** → resolve by session location:

  ```
  git -C "${CLAUDE_PROJECT_DIR}" rev-parse --show-toplevel
  ```

   - **Exit 0** → the session sits inside a repo (worker layout). The printed worktree root **is**
    the target — no question. Go to Step 2 with that path.
   - **Nonzero exit** → `${CLAUDE_PROJECT_DIR}` is not inside any git repo (orchestrator / workspace
     layout). **Hard-fail before any state-changing call**: report that a target is required (a repo
    name or `--all`) and stop. Do not prompt for it — this skill runs `context: fork`; a question
    here has no channel back, the fork has already returned. On a mounted-board orchestrator
    session this hard-fail is the second line behind `orchestrator_freeze.py`, which already
    freezes such a session out of the repos; no code dependency is asserted between the two.

**Bulk branch.** Discover the dirty repos:

```
if command -v rg >/dev/null 2>&1; then
  rg --files -uu --max-depth 6 -g '**/.git/HEAD' "${CLAUDE_PROJECT_DIR}"
else
  find "${CLAUDE_PROJECT_DIR}" -maxdepth 6 -path '*/.git/HEAD'
fi
```

Each hit's grandparent directory is a candidate repo. `git -C <candidate> status --porcelain` per
candidate — non-empty = dirty. Zero dirty → say so, stop. Then **do not spawn an agent per repo** —
hand the whole lot to the script, which does every deterministic part in one sub-second pass:

```
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh plan   --root "${CLAUDE_PROJECT_DIR}"
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh apply  --root "${CLAUDE_PROJECT_DIR}" \
    --messages <file> --no-pr --session-id "${CLAUDE_SESSION_ID}"
```

The plan groups repos by **diff fingerprint** — the common case for `--all` is one identical
distribution across many repos, so N repos share one diff and need one message. Write one
tab-separated `<fingerprint><TAB><message>` line per group (Step 2's convention still applies to
the wording), then apply. `--no-pr` because this skill stops at the commit; `commit-and-pr`
is the one that opens the PR/MR.

Reproduce the script's `=== RESULTS` block verbatim (one `<repo>\t<state>\t-` line per repo;
`--no-pr` so `pr_url` is always `-`), keeping `commit-nopush` distinct from `failed` — the first
means the commit is there, the second that nothing was committed. Derive no state not in the block.

## Step 2 — Commit

1. `git -C <repo> status`, `git -C <repo> diff HEAD`, `git -C <repo> log --oneline -5`.
2. OS noise never goes in a commit. The set is closed — `.DS_Store`, `Thumbs.db`, `desktop.ini`,
  the `NOISE_FILES` constant of `hooks/branch-policy/bulk_commit.sh` — and `resolve_noise()`
  already writes them to `.gitignore` silently, so there is nothing for you to add. Any other path
  is a decision, not noise: ask. In particular **never write a pattern containing `/` or `*`** — a
  directory pattern silently swallows files the repo versions on purpose.
3. **Read** `${CLAUDE_PLUGIN_ROOT}/skills/commit-convention/SKILL.md` — it is the source of truth for
  the emoji/type table and the breaking-change format.
4. Trust your own judgment on grouping and message wording, same as you would writing prose — you
  write better commit messages than a manually-specified rule would produce. Genuinely unrelated
  changes still deserve separate commits; that's a normal editorial call, not a special procedure.
5. **Hook status** → if `bulk_commit.sh plan` reports:
    - `❌ pre-commit failure` -> analyze logs, fix syntax/lint errors, and restart `plan`.
    - `⚠️  missing pre-commit hooks` -> check repo criticality. For POCs/Assets, ignore. For Production, alert user and suggest adding security hooks.
6. Apply the `branch-workflow` skill's Guard section before staging anything — it decides whether
  a feature branch is needed first. Follow what it says, then `git add <files>` and commit with the
  attribution trailer produced by the script (never hand-assembled, never named after your own
  model — you run a different model from the session's, and the script reads the session's):

  ```
  trailer="$(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/commit_trailer.py --session-id "${CLAUDE_SESSION_ID}")"
  git commit -m "<message>" -m "$trailer"
  ```

  The script prints the session's model line (`Co-Authored-By: Claude Sonnet 5 <...>`, or
  `Claude Code (<model>)` for a non-Anthropic model) and, only when
  `$CLAUDE_CODE_BRIDGE_SESSION_ID` is set, the `Claude-Session` line — no broken URL on an empty
  variable. If the session cannot be read it prints the neutral `Co-Authored-By: Claude` line:
  use what it prints, whatever it is. Report `<emoji> <subject>` (+ file list) and the branch you
  committed on.
