---
name: commit
description: Commit the current changes using the gitmoji convention. Bulk mode (--all) commits every dirty repo in one deterministic pass.
argument-hint: "<repository-to-commit> or --all"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(find:*), Bash(rg:*), Bash(git rev-parse:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git checkout:*), Bash(git add:*), Bash(git commit:*), Bash(command -v prek:*), Bash(command -v pre-commit:*), Bash(prek run:*), Bash(pre-commit run:*), Bash(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh:*), AskUserQuestion, Read, Write
---

# Commit — gitmoji convention

Being invoked is the confirmation. Analyse the diff and commit — do not propose and wait, just do
it. (A different trigger — the agent noticing a dirty tree unprompted, end of session — proposes
first per the workspace's commit-proactivity policy; that path never reaches this skill body until
consent already exists.)

## Step 1 — Identify the target(s)

```
git -C "${CLAUDE_PROJECT_DIR}" rev-parse --is-inside-work-tree
```

- Exit 0 → standalone layout. `${CLAUDE_PROJECT_DIR}` itself is the target.
- Nonzero exit → workspace layout:

```
if command -v rg >/dev/null 2>&1; then
  rg --files -uu --max-depth 6 -g '**/.git/HEAD' "${CLAUDE_PROJECT_DIR}"
else
  find "${CLAUDE_PROJECT_DIR}" -maxdepth 6 -path '*/.git/HEAD'
fi
```

  Each hit's grandparent directory is a candidate repo. `git -C <candidate> status --porcelain` per
  candidate — non-empty = dirty.

  - No argument → ask which repo (AskUserQuestion, options = the dirty list). Zero dirty → say so, stop.
  - Argument names one repo → single-repo.
  - Argument names several, or `--all` → bulk: **do not spawn an agent per repo.** Hand the whole
    lot to the script, which does every deterministic part in one sub-second pass:

    ```
    ${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh plan   --root "${CLAUDE_PROJECT_DIR}"
    ${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh apply  --root "${CLAUDE_PROJECT_DIR}" \
        --messages <file> --no-pr
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
2. Known noise files (`.DS_Store`, etc.) never go in a commit — add them to `.gitignore` silently,
  no confirmation needed for this specific action.
3. **Read** `${CLAUDE_PLUGIN_ROOT}/skills/commit-convention/SKILL.md` — it is the source of truth for
  the emoji/type table and the breaking-change format.
4. Trust your own judgment on grouping and message wording, same as you would writing prose — you
  write better commit messages than a manually-specified rule would produce. Genuinely unrelated
  changes still deserve separate commits; that's a normal editorial call, not a special procedure.
5. `command -v prek || command -v pre-commit` — none found → skip to 6. Found → `<runner> run --files
  <changed files>` BEFORE staging (a reformat changes file contents; staging first would commit the
  pre-reformat version). Re-check `git diff HEAD` after — a hook FAILURE (not a reformat) stops you,
  report it, commit nothing.
6. Apply the `branch-workflow` skill's Guard section before staging anything — it decides whether
  a feature branch is needed first. Follow what it says, then `git add <files>` and commit with the
  attribution trailer built programmatically (never hand-assembled — an empty env var must drop the
  line, not emit a broken URL):

   ```
   trailer="Co-Authored-By: Claude <noreply@anthropic.com>"
   [ -n "${CLAUDE_CODE_BRIDGE_SESSION_ID:-}" ] && trailer="$trailer
   Claude-Session: https://claude.ai/code/$CLAUDE_CODE_BRIDGE_SESSION_ID"
   git commit -m "<message>" -m "$trailer"
   ```

   `$CLAUDE_CODE_BRIDGE_SESSION_ID` is inherited from the parent even in a fork. Report
  `<emoji> <subject>` (+ file list) and the branch you committed on.
