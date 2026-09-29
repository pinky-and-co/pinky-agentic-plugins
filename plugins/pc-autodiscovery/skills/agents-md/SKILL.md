---
name: agents-md
description: Seed a repo's AGENTS.md when it has none (folding CONTEXT.md's glossary in), or with --standalone render the full standalone AGENTS.md for a repo leaving the workspace. Never overwrites an existing file without --standalone.
argument-hint: "<repo-path> [--standalone]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/agents-md/agents_md_repo.py":*)
---

# AGENTS.md for one repo

Run the script, then report its stdout verbatim. On a non-zero exit, report stderr verbatim and
stop. Put `--standalone` (when requested) before `--`, and single-quote the repo path:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/agents-md/agents_md_repo.py" [--standalone] -- '<repo-path>'
```

Write an embedded single quote as `'\''`. Never splice the raw argument string into the command
line.

- Without `--standalone`: creates `<repo>/AGENTS.md` only when absent. The repo needs a `.git`
  entry.
- With `--standalone`: overwrites it atomically; only the hand-authored body survives. The
  workspace is resolved from `PINKY_WORKSPACE_ROOT` or the nearest `*.code-workspace` (never a
  repo nor `$HOME`); add `--workspace '<path>'` before `--` to force it.
- The hook that keeps workspace and group AGENTS.md files in sync replaces any hand-written `##`
  section in them (only the title and intro survive). Repo-level files are hand-authored and are
  never regenerated.
- The SessionStart hook does nothing on a `python3` older than 3.11 (no `tomllib`): a stale
  workspace or group AGENTS.md on such a machine means the hook never ran.
