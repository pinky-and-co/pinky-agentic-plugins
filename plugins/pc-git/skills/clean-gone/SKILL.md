---
name: clean-gone
description: Delete local git branches marked [gone] (deleted on the remote, e.g. after a merge) and their worktrees. Auto — nothing here is destructive to unmerged work, [gone] means the remote already dropped it. --all sweeps every repo under the workspace root in one pass, symmetric with commit-and-pr --all.
argument-hint: "<repository-name | /absolute/repo/path> or --all"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/skills/clean-gone/scripts/bulk_clean_gone.sh:*), Bash(test -e:*)
---

# Clean up merged/gone branches

Being invoked is the confirmation — this only touches branches the remote has already deleted,
never anything with unpushed or unmerged work. No confirmation needed.

`scripts/bulk_clean_gone.sh` owns everything deterministic: `fetch --prune` before ever reading
`[gone]` status, fast-forwarding the base branch (checked out or not), switching HEAD to the base
only when HEAD itself is `[gone]`, and removing any worktree tied to a branch it deletes. A branch
pushed without `-u` has no upstream and never shows `[gone]`; it counts as gone when `origin/<name>`
is absent and its tip is the second parent of a merge on `origin/<base>`. The `pc-git` SessionStart
hook runs the same script on the session's own repo. No subagent per repo — one process, one pass.

```bash
${CLAUDE_PLUGIN_ROOT}/skills/clean-gone/scripts/bulk_clean_gone.sh --root "${CLAUDE_PROJECT_DIR}" [--repo <name>]
```

`args` is whatever trails an `ARGUMENTS: ...` line at the very end of this prompt. **No such line
means no argument was passed** — the normal shape for a bare invocation: the whole root, same as
`--all` below, never a reason to ask.

A named repo as argument → add `--repo <name>` (basename match against every repo the same
discovery finds — `find <root> -maxdepth 4 -name .git`, `node_modules` excluded, identical to
`bulk_commit.sh`'s). No argument or `--all` → no `--repo`, the whole root.

An absolute path (starts with `/`) combined with `--all` or with any other repo name or path: stop
with a plain error, run nothing. Alone, it must be exactly a repo's own directory: run `test -e "<path>/.git"`
(quote the path). If it fails — a subdirectory of a repo, or a directory that merely contains repos,
is not a repo path — say so plainly and stop, never guess another repo. Otherwise run the script above with `--repo-path "<path>"` **instead of** both `--root` and
`--repo` (the script then acts on that one repo alone, nested repos excluded). An explicit argument
always wins over the session location.

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
