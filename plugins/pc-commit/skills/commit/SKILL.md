---
name: commit
description: Commit the current changes using the gitmoji convention. Also invoked as a per-repo child during --all/bulk mode.
argument-hint: "<repository-to-commit> or --all"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(find:*), Bash(rg:*), Bash(git rev-parse:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git add:*), Bash(git commit:*), Bash(command -v prek:*), Bash(command -v pre-commit:*), Bash(prek run:*), Bash(pre-commit run:*), AskUserQuestion, Read, Agent
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
  - Argument names several, or `--all` → bulk: fork one child per dirty repo (`Agent`, `context:
    fork`, same skill). Each child runs this skill for its own repo and reports one line —
    `<repo>: <emoji> <subject>` (or the real error, never swallowed). No batch table, no shared
    confirmation to wait for — each child commits as soon as it's done analysing.

## Step 2 — Commit

1. `git -C <repo> status`, `git -C <repo> diff HEAD`, `git -C <repo> log --oneline -5`.
2. Known noise files (`.DS_Store`, etc.) never go in a commit — add them to `.gitignore` silently,
  no confirmation needed for this specific action.
3. **Read** `references/commit-convention.md` — it is the source of truth for the emoji/type table,
  not what training data recalls about gitmoji.dev in general.
4. Trust your own judgment on grouping and message wording, same as you would writing prose — you
  write better commit messages than a manually-specified rule would produce. Genuinely unrelated
  changes still deserve separate commits; that's a normal editorial call, not a special procedure.
5. `command -v prek || command -v pre-commit` — none found → skip to 6. Found → `<runner> run --files
  <changed files>` BEFORE staging (a reformat changes file contents; staging first would commit the
  pre-reformat version). Re-check `git diff HEAD` after — a hook FAILURE (not a reformat) stops you,
  report it, commit nothing.
6. `git add <files> && git commit -m "<message>"`. Report `<emoji> <subject>` (+ file list).
