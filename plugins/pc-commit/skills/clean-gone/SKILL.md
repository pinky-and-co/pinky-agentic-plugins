---
name: clean-gone
description: Delete local git branches marked [gone] (deleted on the remote, e.g. after a merge) and their worktrees. Auto — nothing here is destructive to unmerged work, [gone] means the remote already dropped it.
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(git branch:*), Bash(git worktree:*)
---

# Clean up merged/gone branches

Being invoked is the confirmation — this only touches branches the remote has already deleted
(post-merge or otherwise), never anything with unpushed or unmerged work. No confirmation needed.

1. `git -C "${CLAUDE_PROJECT_DIR}" branch -v` — identify `[gone]` branches. A `+` prefix means an
   associated worktree exists and must be removed before the branch can be deleted.
2. `git -C "${CLAUDE_PROJECT_DIR}" worktree list` — match worktrees to `[gone]` branches.
3. For each `[gone]` branch: remove its worktree if any (`git worktree remove --force <path>`), then
   delete the branch (`git branch -D <branch>`).
4. Report which branches/worktrees were removed. None found → say so, nothing to do.
