---
name: commit-and-pr
description: Commit, push, and open a PR (GitHub) or MR (GitLab) — full flow, auto end-to-end. Bulk mode (--all) handles every dirty repo in one deterministic pass. Use when the user asks to commit, push and open a PR or MR in one go.
argument-hint: "<repository-to-commit | /absolute/repo/path> or --all [--no-watch-ci]"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh:*), Bash(git rev-parse:*), Bash(git -C * rev-parse*), Bash(test -e:*), Bash(git status:*), Bash(git -C * status*), Bash(rtk git -C * status*), Bash(rtk git status:*), Bash(git diff:*), Bash(git -C * diff*), Bash(rtk git -C * diff*), Bash(rtk git diff:*), Bash(git log:*), Bash(git -C * log*), Bash(rtk git -C * log*), Bash(rtk git log:*), Bash(gh:*), Bash(rtk gh:*), Bash(glab:*), Bash(rtk glab:*), Bash(jq:*), Bash(rtk jq:*), Bash(mktemp:*), Read, Write, Bash(prek run:*), Bash(pre-commit run:*), Bash(rtk pre-commit run:*)
---

# Commit + push + PR/MR

**You are running unattended, right now, with no user watching and no separate task message
coming.** This document IS the task — reading it is the instruction to start Step 1 in your very
first tool call. Do not introduce yourself, do not summarize the working tree, do not ask what to
do: those all cost a turn nobody will answer.

Being invoked is the confirmation for the whole chain, commit through PR/MR — do not pause partway.
The PR/MR itself is where review happens once it exists; there is nothing to gate before that.

## What you actually do here

`hooks/branch-policy/bulk_commit.sh` owns everything deterministic: discovering dirty repos, resolving OS noise
files into `.gitignore`, staging, grouping repos by diff fingerprint, resolving the branch policy,
applying the branch guard, committing, pushing, opening the PR/MR, and reporting per-stage outcomes.

You do the one thing it cannot: **write the commit messages**. Nothing else. In particular, do not
spawn a subagent per repo — the script handles every repo in one pass: commit/push/PR is under a
second per repo, CI watching (on by default, see Step 3) adds real wall time on top of that.

## Step 1 — Resolve the scope (read-only), then plan

`args` is whatever trails an `ARGUMENTS: ...` line at the very end of this prompt. **No such line
at all means no argument was passed** — that is the normal, expected shape for a bare invocation,
not missing information. Go straight to the "No argument" branch below; never ask for one instead.

**First, strip a trailing `--no-watch-ci` from `args`** and remember whether it was there — Step 3
acts on it. Match what *remains* against a repo name / an absolute repo path / `--all`: matching the raw string (e.g.
`myrepo --no-watch-ci`) would never equal `--all` nor appear in `discover`'s output, so it would
fall into the "not in the list" branch instead of resolving. CI watching is the default; this flag
is an opt-*out*.

**Then resolve the target:**

- **An explicit argument** — a repo name, an absolute repo path or `--all` — always wins. Use it.
- **An absolute repo path** (starts with `/`) — combined with `--all` or with any other repo name or
  path, stop with a plain error before any tool call. Otherwise the path must be exactly a repo's
  own directory (a `.git` entry, file or directory): run `test -e "<path>/.git"`. If it fails — a
  subdirectory of a repo, or a directory that merely contains repos, is not a repo path — say so
  plainly and stop, never guess another repo. Then **quote the path** and, in each of `discover`,
  `plan` and `apply` below, write `--repo-path "<path>"` **instead of** `--root "${CLAUDE_PROJECT_DIR}"`
  and instead of `--repo <target>` (the script then acts on that one repo alone, nested repos
  excluded). The target is the path's basename.
- **No argument** → resolve by session location:

  ```
  git -C "${CLAUDE_PROJECT_DIR}" rev-parse --show-toplevel
  ```

  - **Exit 0** → the session sits inside a repo (worker layout). Target = the basename of the
    printed worktree root — no question. In a worker session `${CLAUDE_PROJECT_DIR}` *is* that
    worktree root, so `--root "${CLAUDE_PROJECT_DIR}"` in every command below resolves to this same
    repo. If the two ever differ (`${CLAUDE_PROJECT_DIR}` is a subdirectory — `discover` returns
    `NO-REPOS-FOUND` though `--show-toplevel` succeeded), substitute the `--show-toplevel` path for
    `${CLAUDE_PROJECT_DIR}` in **every** `--root` below.
  - **Nonzero exit** → `${CLAUDE_PROJECT_DIR}` is not inside any git repo (orchestrator / workspace
    layout). **Hard-fail before any tool call**: report that a target is required (a repo name or
    `--all`) and stop — never guess one. Do not prompt for it: this skill runs `context: fork`, a
    question has no channel back. On a mounted-board orchestrator session this hard-fail is the
    second line behind `orchestrator_freeze.py`, which already freezes such a session out of the
    repos; no code dependency is asserted between the two.

**Never run `plan` unfiltered to "see what's there"** — `plan` stages (`git add -A`) every repo it
scans. Confirm the resolved scope with the read-only subcommand first:

```
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh discover --root "${CLAUDE_PROJECT_DIR}"
```

It prints one dirty-repo basename per line, stages nothing. Match it against the target resolved
above:

- **`NO-REPOS-FOUND`** (non-zero exit) → **do not report success**: no git repo under the root at
  all, so it is stale (a moved workspace is the usual cause). Say the root is wrong and stop —
  "nothing to commit" and "I looked in the wrong place" are not the same answer.
- **empty list, exit 0** → "nothing to commit", stop.
- **the resolved target is a repo name not in the list** → say so, stop. Never guess a closest
  match.
- **the resolved target is one repo from the list** (named explicitly, or resolved from the session
  location) → target = that repo.
- **`--all`** → target = *whole root* (no `--repo`). `--all` is never passed to the script; it just
  means "omit `--repo`".

Then plan the resolved target:

```
    ${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh plan --root "${CLAUDE_PROJECT_DIR}" [--repo <target>]


    **Hook status** → if `plan` reports:
       - `❌ pre-commit failure` $ightarrow$ analyze logs, fix syntax/lint errors, and restart `plan`.
       - `⚠️  missing pre-commit hooks` $ightarrow$ check repo criticality. For POCs/Assets, ignore. For Production, alert user and suggest adding security hooks.
```

Include `--repo <target>` for every scope except `--all` / `all of them` and except path mode (use
`--repo-path "<path>"` alone, never with `--root`/`--repo`).

- `NO-REPOS-FOUND` → **do not report success**: the root holds no git repo at all, so it is stale
  (a moved workspace is the usual cause). Say the root is wrong and stop.

The plan groups repos by **diff fingerprint**: the common case for `--all` is one identical
distribution landing on many repos, so N repos share one diff and need one message. Twenty repos
usually come back as two or three groups.

Each group carries its fingerprint, the file list, the shared diff, and per repo the resolved branch
strategy plus whether the guard requires a feature branch. A group whose fingerprint is
`noise-gitignore` is already handled — it gets a fixed message, do not write one for it.

## Step 2 — One message per GROUP, not per repo

**Read** `${CLAUDE_PLUGIN_ROOT}/skills/commit-convention/SKILL.md` — it is the source of truth for
the emoji/type table and the breaking-change format. Judge grouping and wording from the diff the
group shows.

Write one tab-separated line per group to a scratch file:

```
<fingerprint><TAB><emoji> <type>: <subject>
```

A message describes the change, so a shared diff legitimately yields one shared message — that is
correct, not a shortcut.

## Step 3 — Apply

```
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh apply --root "${CLAUDE_PROJECT_DIR}" --messages - --session-id "${CLAUDE_SESSION_ID}" [--repo <target>] [--no-watch-ci] <<'EOF'
<fingerprint><TAB><message>
EOF
```

Messages go in on **standard input** (`--messages -` plus a `<<'EOF'` heredoc) — never write a message file: a file under `/tmp` is outside the working directory and asks for a permission, and neither `cat > file` nor `printf > file` is allowed.

`--session-id "${CLAUDE_SESSION_ID}"` is always passed, in every scope (path mode included): the
script reads the session's model from that session's transcript to name it in the commit trailer —
your own model, the fork's, is not the one that wrote the code.

Pass the **same `--repo <target>`** you resolved in Step 1 — omit it only for `--all` / `all of
them`, and in path mode (`--repo-path "<path>"` alone, never with `--root`/`--repo`). Add `--dry-run` first if the plan looked surprising — it prints what each repo would get,
touching nothing, and emits **no `=== RESULTS` block** (run the real apply for that).

**CI watching is on by default** — pass `--no-watch-ci` **if and only if Step 1 found it trailing
`args`**; that parsed flag is the source of truth, not a fresh guess here. Left on, the script
watches CI on each PR/MR it opened and appends a `=== CI` block; with `--no-watch-ci` it never calls
`gh pr checks`/`glab ci`/`glab api` and the run ends at PR/MR creation. `--watch-ci` is still
accepted (a no-op restating the default).

Watching is **parallel, in batches of `BULK_CI_PARALLEL`** (default 4), not one PR after another —
`--all` no longer pays up to 15 min × N repos sequentially; the ceiling is
`⌈N / BULK_CI_PARALLEL⌉ × (BULK_CI_STARTUP_DELAY + BULK_CI_TIMEOUT)`, reached only if every repo in a
batch stalls to the timeout. `BULK_CI_STARTUP_DELAY` (default 60s) is a fixed wait each watch takes
before its first probe, to let CI register — that delay is expected, not a hang.

- **GitHub** — chains `gh pr checks --watch --fail-fast`.
- **GitLab** — `glab` has no equivalent primitive with a trustworthy exit code, so the script reads
  the status of the MR's own pipeline through `glab api` instead of trusting `glab ci status --wait`.

## Step 4 — Report what the script reported

The script prints a `=== RESULTS` block, one tab-separated line per repo — `<repo>\t<state>\t<pr_url|->`.
**Reproduce those lines. Derive no sentence that is not in them** — in particular never write "PR
created" / "PR opened" for a repo whose `state` is not `pushed-pr`. The forked agent that broke this
filled the script's silence with a guess; the block exists so there is no silence to fill.

| `state` | Meaning | `pr_url` |
|---|---|---|
| `pushed-pr` | committed, pushed, PR/MR exists | the URL |
| `pushed-nopr` | committed + pushed straight to the integration branch (unguarded paths), or `--no-pr` | `-` |
| `pushed-prfailed` | committed + pushed on a feature branch, but PR creation failed — **a human must open it** | `-` |
| `commit-nopush` | committed locally, push failed — the commit is there, nothing lost | `-` |
| `push-conflict` | committed locally, push rejected and the script's own fetch+rebase hit a real conflict — it already ran `rebase --abort`, the repo is clean | path to the list of conflicting files (not a URL) |
| `skipped` | no message matched this group's fingerprint | `-` |
| `failed` | branch or commit failed — changes remain staged, nothing lost | `-` |

Never flatten `commit-nopush`/`pushed-prfailed`/`push-conflict` into `failed`, and never report
`pushed-pr` for a repo the block did not. A run that claims success while leaving work on the floor
is worse than one that fails loudly. If any line is `push-conflict`, tell the parent to read
`${CLAUDE_PLUGIN_ROOT}/skills/commit-and-pr/references/conflict-reaction.md` and that the
conflicting-files list is at the path in column 3.

After `=== END RESULTS`, a `=== FILES` … `=== END FILES` block lists the files published, one
`<repo>\t<path>` line each, for every repo whose `state` is `pushed-pr`, `pushed-nopr` or
`pushed-prfailed` (none for `commit-nopush`, `push-conflict`, `skipped`, `failed`; the block is absent
when no repo was pushed). **Relay it verbatim, like `RESULTS` and `CI`.** The parent compares it with
what it meant to publish — an unexpected path means a stray file went out.

## Step 5 — Report the CI verdicts (skip only if `--no-watch-ci` was passed)

Unless `--no-watch-ci` was passed, the script appends a block, `=== CI` … `=== END CI`, one
tab-separated line per PR/MR it watched — `<repo>\t<verdict>\t<url>\t<log_path|->`. **Reproduce those
lines too. Derive nothing.**

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
