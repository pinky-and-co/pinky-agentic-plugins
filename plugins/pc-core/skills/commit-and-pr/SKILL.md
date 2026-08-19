---
name: commit-and-pr
description: Commit, push, and open a PR (GitHub) or MR (GitLab) — full flow, auto end-to-end. Bulk mode (--all) handles every dirty repo in one deterministic pass.
argument-hint: "<repository-to-commit> or --all"
disable-model-invocation: true
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

## Step 1 — Plan

```
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh plan --root "${CLAUDE_PROJECT_DIR}"
```

One repo named as argument → add `--repo <name>`. `--all` or no argument → the whole root.
Output `NOTHING-TO-COMMIT` → say so and stop. Output `NO-REPOS-FOUND` → **do not report
success**: the root holds no git repo at all, so it is stale (a moved workspace is the usual
cause). Say the root is wrong and stop; "nothing to commit" and "I looked in the wrong place"
are not the same answer.

The plan groups repos by **diff fingerprint**, and that grouping is the point: the common case for
`--all` is one identical distribution landing on many repos, so N repos share one diff and need one
message. Twenty repos usually come back as two or three groups.

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
${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh apply --root "${CLAUDE_PROJECT_DIR}" --messages <file>
```

Add `--dry-run` first if the plan looked surprising — it prints what each repo would get, touching
nothing.

## Step 4 — Report what the script reported

Pass its outcome through verbatim, per repo. The four states are distinct and must stay distinct:

| State | Meaning |
|---|---|
| ✅ | committed, pushed, PR/MR handled |
| ⚠️ | committed, push failed — the commit is there, nothing is lost |
| ⏭️ | no message matched this group's fingerprint |
| ❌ | branch or commit failed — changes remain staged, nothing is lost |

Never flatten ⚠️ into ❌, and never report ✅ for a repo the script did not mark ✅. A bulk run that
claims success while leaving work on the floor is worse than one that fails loudly.
