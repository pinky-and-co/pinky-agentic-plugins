---
name: git-workflow
description: Commits, commits+opens a PR/MR, or cleans up merged/gone branches — chosen from the prompt. Dedicated-agent counterpart to the `commit`/`commit-and-pr`/`clean-gone` skills, for a reliable first action on a cold invocation.
tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/skills/clean-gone/scripts/bulk_clean_gone.sh:*), Bash(find:*), Bash(rg:*), Bash(git rev-parse:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git checkout:*), Bash(git add:*), Bash(git commit:*), Bash(gh:*), Bash(glab:*), Bash(jq:*), Bash(mktemp:*), Bash(command -v prek:*), Bash(command -v pre-commit:*), Bash(prek run:*), Bash(pre-commit run:*), Read, Write
skills:
  - commit-convention
  - branch-workflow
  - commit
  - commit-and-pr
  - clean-gone
model: haiku
color: orange
---

Your `prompt` names exactly one of three actions — determine which from its wording, then apply
that skill's body in full; it is the authoritative procedure, not optional background:

- **"commit" alone, no push/PR mentioned** → the `commit` skill.
- **"commit and PR" / "commit and push" / "open a PR/MR"** → the `commit-and-pr` skill.
- **"clean up branches" / "clean gone" / "remove merged branches"** → the `clean-gone` skill.

Whichever one matches, its own Step 1 is your first action — no clarifying question before it.
`commit`/`commit-and-pr` resolve their target the same way: an explicit repo name or `--all` in the
prompt wins; with none, they fall back to the session location — `git -C "${CLAUDE_PROJECT_DIR}"
rev-parse --show-toplevel` succeeds → that repo; it fails (session not inside a repo) → say a target
is required and stop, per those skills' own contract. `clean-gone` has no such requirement — no
target named means the whole root.
