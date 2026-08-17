---
name: commit-and-pr
description: Commit, push, and open a PR (GitHub) or MR (GitLab) — full flow, auto end-to-end. Also invoked as a per-repo child during --all/bulk mode.
argument-hint: "<repository-to-commit> or --all"
disable-model-invocation: true
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(find:*), Bash(rg:*), Bash(git rev-parse:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git remote:*), Bash(git checkout:*), Bash(git add:*), Bash(git commit:*), Bash(git push:*), Bash(command -v prek:*), Bash(command -v pre-commit:*), Bash(prek run:*), Bash(pre-commit run:*), Bash(gh pr view:*), Bash(gh pr create:*), Bash(glab mr view:*), Bash(glab mr create:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/resolve_policy.py:*), AskUserQuestion, Read, Agent
---

# Commit + push + PR/MR

Being invoked is the confirmation for the whole chain, commit through PR/MR — do not pause partway.
The PR/MR itself is where review happens once it exists; there is nothing to gate before that.

## Step 1 — Identify the target(s), same as `commit`

```
git -C "${CLAUDE_PROJECT_DIR}" rev-parse --is-inside-work-tree
```

Standalone → `${CLAUDE_PROJECT_DIR}` is the target. Workspace layout → discover dirty repos via
`rg --files -uu --max-depth 6 -g '**/.git/HEAD' "${CLAUDE_PROJECT_DIR}"` (grandparent dir of each
hit, `git status --porcelain` to confirm dirty). No argument → ask (AskUserQuestion, options = dirty
list). One repo named → single-repo. Several / `--all` → spawn one child per dirty repo via the
`Agent` tool (`subagent_type: "general-purpose"`, `model: "haiku"` — this skill already runs inside
a fork, and a fork cannot spawn another fork). Prompt each child with: the repo's absolute path, the
resolved absolute path of `${CLAUDE_PLUGIN_ROOT}` (a plain subagent doesn't inherit it — pass the
literal value, not the variable), and "execute Steps 2 through 4 of this file for that repo". Each
child runs independently and reports one line when done — `<repo>: <emoji> <subject> — <PR/MR
URL>`. No batch table to build first: nothing here needs a shared confirmation to wait for.

## Step 2 — Provider detection (a fact, never a guess)

```
git -C <repo> remote get-url origin
```

URL contains `gitlab` (`gitlab.com` or self-hosted) → GitLab, use `glab mr ...`. Otherwise →
GitHub, use `gh pr ...`. This is read off the remote, never inferred from anything else.

## Step 3 — Commit

Same as `commit`: collect context (`status`/`diff HEAD`/`log --oneline -5`/`branch --show-current`),
resolve noise files into `.gitignore` silently, **Read** `references/commit-convention.md` (source
of truth for the emoji/type table — training data's generic gitmoji knowledge is not), trust your
own judgment on grouping/wording, run `prek`/`pre-commit` before staging if present (re-check the
diff after — a real failure stops you here, propose nothing further for this repo), then
`git add && git commit`.

## Step 4 — Branch + PR/MR

1. Branch strategy for `${CLAUDE_PROJECT_DIR}`, resolved before you read this line — not a prose
   instruction to follow, a value already looked up: `` !`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/resolve_policy.py" branch_workflow --repo "${CLAUDE_PROJECT_DIR}"` ``.
   Empty output → no `.claude/policies.json` declared for this repo; don't guess — fall back to
   `git merge-base` against likely candidates and flag the missing declaration in your report.
   Bulk mode only (a child forked for a repo other than `${CLAUDE_PROJECT_DIR}`): this DCI value is
   stale (resolved once, for the parent's own directory, before any fork existed) — re-resolve with
   a real tool call instead: `Bash("${CLAUDE_PLUGIN_ROOT}/hooks/resolve_policy.py" branch_workflow --repo <your repo>)`.
2. Apply `branch-workflow`'s Guard section, using the strategy resolved above — it decides whether
   a feature branch is needed before you commit (do this before Step 3, not after).
3. Push: `git push -u origin <branch>`.
4. Check for an existing open PR/MR on this branch first — `gh pr view --json state,url` or
   `glab mr view --json` per the provider from Step 2. One already open → report its URL, stop, never
   open a second one.
5. None exists → open one, base branch = the strategy resolved in Step 4.1, never hardcoded
   `develop`/`main`. `gh pr create --base <base> --title "<title>" --body "<Summary + Test plan>"` or
   `glab mr create --target-branch <base> --title "<title>" --description "..."`.
6. Report the PR/MR URL.
