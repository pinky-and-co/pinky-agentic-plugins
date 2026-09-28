---
name: clean-gone
description: Delete local git branches marked [gone] (deleted on the remote, e.g. after a merge) and their worktrees. Auto — nothing here is destructive to unmerged work, [gone] means the remote already dropped it. --all sweeps every repo under the workspace root in one pass, symmetric with commit-and-pr --all.
argument-hint: "<repository-name> or --all"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/skills/clean-gone/scripts/bulk_clean_gone.sh:*)
---

# Clean up merged/gone branches

Being invoked is the confirmation — this only touches branches the remote has already deleted
(post-merge or otherwise), never anything with unpushed or unmerged work. No confirmation needed.

`scripts/bulk_clean_gone.sh` owns everything deterministic: `fetch --prune` before ever reading
`[gone]` status (a branch deleted on GitHub/GitLab stays invisible locally until this runs),
fast-forwarding the base branch (checked out or not), switching HEAD to the base only when HEAD
itself is `[gone]`, and removing any worktree tied to a branch it deletes. A branch pushed without
`-u` has no upstream and never shows `[gone]`; it counts as gone when `origin/<name>` is absent and
its tip is the second parent of a merge on `origin/<base>`. The `pc-git` SessionStart hook runs the
same script on the session's own repo. No subagent per repo — one process, one pass, same shape
as `commit-and-pr`'s `bulk_commit.sh`.

```bash
${CLAUDE_PLUGIN_ROOT}/skills/clean-gone/scripts/bulk_clean_gone.sh --root "${CLAUDE_PROJECT_DIR}" [--repo <name>]
```

`args` is whatever trails an `ARGUMENTS: ...` line at the very end of this prompt. **No such line
at all means no argument was passed** — that is the normal, expected shape for a bare invocation,
not missing information: it means the whole root, same as `--all` below, never a reason to ask.

A named repo as argument → add `--repo <name>` (basename match against every repo the same
discovery finds — `find <root> -maxdepth 4 -name .git`, `node_modules` excluded, identical to
`bulk_commit.sh`'s, reused not reinvented). No argument or `--all` → no `--repo`, the whole root.

## Report

Reproduce the script's stdout **verbatim** in one fenced code block — same lines, same order. It is already the complete report: per repo, what changed (HEAD moved, base
commits gained, branches removed by name) or `nothing to clean`, then a final `summary:` line.

- **Add nothing.** No lead-in ("Done."), no closing recap, no bold, no table, no prose rewrite, no
  translation.
- **Count nothing.** The `summary:` line is the only tally, and it mixes units on purpose: the first
  three counters are **repos**, the last one is **branches**. Never restate them in another unit
  ("1 branch cleaned" from a `1 repo(s) cleaned` is a guess).
- **Exit code first.** A non-zero exit means the run failed: reproduce the `❌` lines, then the exit
  code and whatever the script wrote on stderr (`clean-gone: …`). Never describe a non-zero run as a
  success.
- Empty stdout with exit 0 → say exactly that; never fill the silence with a recap.
