---
name: commit-and-pr
description: Commit, push, and open a PR (GitHub) or MR (GitLab) — full flow, auto end-to-end. Bulk mode (--all) handles every dirty repo in one deterministic pass.
argument-hint: "<repository-to-commit> or --all"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Read, Write, AskUserQuestion
---

# Commit + push + PR/MR

Being invoked is the confirmation for the whole chain, commit through PR/MR — do not pause partway.
The PR/MR itself is where review happens once it exists; there is nothing to gate before that.

## What you actually do here

`hooks/branch-policy/bulk_commit.sh` owns everything deterministic: discovering dirty repos, resolving OS noise
files into `.gitignore`, staging, grouping repos by diff fingerprint, resolving the branch policy,
applying the branch guard, committing, pushing, opening the PR/MR, and reporting per-stage outcomes.

You do the one thing it cannot: **write the commit messages**. Nothing else. In particular, do not
spawn a subagent per repo — the script handles every repo in one pass, in under a second.

## Step 1 — Resolve the scope (read-only), then plan

**Never run `plan` unfiltered to "see what's there"** — `plan` stages (`git add -A`) every repo it
scans. Discover the scope with the read-only subcommand first:

```
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh discover --root "${CLAUDE_PROJECT_DIR}"
```

It prints one dirty-repo basename per line, stages nothing. From that list + the argument:

- **`NO-REPOS-FOUND`** (non-zero exit) → **do not report success**: no git repo under the root at
  all, so it is stale (a moved workspace is the usual cause). Say the root is wrong and stop —
  "nothing to commit" and "I looked in the wrong place" are not the same answer.
- **empty list, exit 0** → "nothing to commit", stop.
- **argument names one repo** → target = that repo.
- **`--all`** → target = *whole root* (no `--repo`). `--all` is never passed to the script; it just
  means "omit `--repo`".
- **no argument, one line** → target = that repo.
- **no argument, more than one line** → never sweep (an unqualified run once committed 7 unrelated
  dirty repos and reported PRs that were never opened). `AskUserQuestion`:
  1. "One specific repo, or all of them?" → `just one` / `all of them` / `stop`.
  2. if `just one`: offer the first 4 basenames plus `none of these`; on `none of these`, ask again
     with the next 4, until one is picked.

  `all of them` *is* the consent for a whole-root run. `stop` → do nothing.

Then plan the resolved target:

```
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh plan --root "${CLAUDE_PROJECT_DIR}" [--repo <target>]
```

Include `--repo <target>` for every scope except `--all` / `all of them`.

- `NO-REPOS-FOUND` → **do not report success**: the root holds no git repo at all, so it is stale
  (a moved workspace is the usual cause). Say the root is wrong and stop.

The plan groups repos by **diff fingerprint**: the common case for `--all` is one identical
distribution landing on many repos, so N repos share one diff and need one message. Twenty repos
usually come back as two or three groups.

Each group carries its fingerprint, the file list, the shared diff, and per repo the resolved branch
strategy plus whether the guard requires a feature branch. A group whose fingerprint is
`noise-gitignore` is already handled — it gets a fixed message, do not write one for it.

## Step 2 — One message per GROUP, not per repo

**Read** `references/commit-convention.md` — it is the source of truth for the emoji/type table;
training-data gitmoji knowledge is not. Judge grouping and wording from the diff the group shows.

Write one tab-separated line per group to a scratch file:

```
<fingerprint><TAB><emoji> <type>: <subject>
```

A message describes the change, so a shared diff legitimately yields one shared message — that is
correct, not a shortcut.

## Step 3 — Apply

```
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh apply --root "${CLAUDE_PROJECT_DIR}" --messages <file> [--repo <target>] [--watch-ci]
```

Pass the **same `--repo <target>`** you resolved in Step 1 — omit it only for `--all` / `all of
them`. Add `--dry-run` first if the plan looked surprising — it prints what each repo would get,
touching nothing, and emits **no `=== RESULTS` block** (run the real apply for that).

Pass **`--watch-ci` only when the invocation asked for it** (the parent wants the CI verdict, not
just "PR/MR opened"). It makes the script watch CI on each PR/MR it opened — up to 15 min per PR/MR —
and append a `=== CI` block. Without the flag the script never calls `gh pr checks`/`glab ci`/`glab
api` and the run ends at PR/MR creation, exactly as before.

- **GitHub** — chains `gh pr checks --watch --fail-fast`.
- **GitLab** — `glab` has no equivalent primitive with a trustworthy exit code, so the script reads
  the status of the MR's own pipeline through `glab api` instead of trusting `glab ci status --wait`.

## Step 4 — Report what the script reported

The script ends with a `=== RESULTS` block, one tab-separated line per repo — `<repo>\t<state>\t<pr_url|->`.
**Reproduce those lines. Derive no sentence that is not in them** — in particular never write "PR
created" / "PR opened" for a repo whose `state` is not `pushed-pr`. The forked agent that broke this
filled the script's silence with a guess; the block exists so there is no silence to fill.

| `state` | Meaning | `pr_url` |
|---|---|---|
| `pushed-pr` | committed, pushed, PR/MR exists | the URL |
| `pushed-nopr` | committed + pushed straight to the integration branch (unguarded paths), or `--no-pr` | `-` |
| `pushed-prfailed` | committed + pushed on a feature branch, but PR creation failed — **a human must open it** | `-` |
| `commit-nopush` | committed locally, push failed — the commit is there, nothing lost | `-` |
| `skipped` | no message matched this group's fingerprint | `-` |
| `failed` | branch or commit failed — changes remain staged, nothing lost | `-` |

Never flatten `commit-nopush`/`pushed-prfailed` into `failed`, and never report `pushed-pr` for a
repo the block did not. A run that claims success while leaving work on the floor is worse than one
that fails loudly.

## Step 5 — Report the CI verdicts (only when `--watch-ci` was passed)

With `--watch-ci` the script appends a second block, `=== CI` … `=== END CI`, one tab-separated
line per PR/MR it watched — `<repo>\t<verdict>\t<url>\t<log_path|->`. **Reproduce those lines too.
Derive nothing.**

| `verdict` | Meaning | `log_path` |
|---|---|---|
| `pass` | every check green (GitLab: includes a pipeline blocked on a `manual` job — a process gate, not a CI failure) | `-` |
| `fail` | a check failed (GitHub: `--fail-fast` stopped the watch · GitLab: pipeline `status` is `failed`/`canceled`) | GitHub: ~80-line extract of `gh run view --log-failed` · GitLab: ~80-line extract of `glab api .../trace` for the first failed job — `-` either way if the failing run/job could not be retrieved |
| `no-ci` | PR/MR open, no checks/pipeline for it — **not** a pass, nothing was waited for | `-` |
| `timeout` | CI still running after 15 min/PR — verdict unknown, PR/MR is open (GitLab: also covers an unreadable pipeline status — auth failure, project not found — since that is not distinguishable from "still running" without a human checking `glab auth status`) | `-` |

Do **not** fix anything here — this skill runs in a fork without the code context. Hand the block
back to the parent. If any line is `fail`, tell the parent to read
`${CLAUDE_PLUGIN_ROOT}/skills/commit-and-pr/references/ci-reaction.md` and that the log extract is
at the path in column 4.
