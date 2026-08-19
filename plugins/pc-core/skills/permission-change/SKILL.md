---
name: permission-change
description: Toggle the PreToolUse permission guards — careful (confirm destructive Bash), freeze (lock edits to a scope), deny (block edits to a scope), or guard (careful + freeze together)
model: haiku
allowed-tools: Bash(python3:*), Bash(py:*)
---

# /permission-change — one entry point for the permission guards

Four related guards, one command — `--mode=` picks which. All four are enforced by the same
PreToolUse hook (`guard.py`) and share one state file; the mode you pick decides what gets
written to it.

## Modes

- **`--mode=careful [on|off]`** — while on, a destructive `Bash` command (`rm -rf`,
  `git push --force`, `git reset --hard`, `DROP`, `TRUNCATE`, `DELETE FROM` without `WHERE`,
  `sudo`, …) returns `permissionDecision: ask` instead of running silently. A speed bump, not
  a block.
- **`--mode=freeze <dir>... | off`** — a whitelist: locks `Edit`/`Write`/`MultiEdit`/
  `NotebookEdit` to the given directories; anything outside is denied.
- **`--mode=deny <dir>... | off`** — the inverse of `freeze`: a blocklist. The given
  directories become unwritable; everything else stays open. Not the same as the
  `permissions.deny` settings key (harder guarantee, session-start only, not a toggle).
- **`--mode=guard <dir>... | on | off`** — convenience: `careful` on + `freeze` to the given
  directories in one call.

**Honest limit, all four**: only the write-class TOOLS are covered. A Bash write (`sed -i`,
`tee`, `>`, `mv`/`cp`) is NOT intercepted — for a hard guarantee, use `permissions.deny` instead.

## Action

`$ARGUMENTS` is the mode flag plus whatever that mode takes — pass it straight through, do not
reinterpret it.

Run the control script (use `py -3` instead of `python3` on Windows):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py" $ARGUMENTS
```

Examples: `/permission-change --mode=careful on`, `/permission-change --mode=freeze src/core`,
`/permission-change --mode=deny secrets/`, `/permission-change --mode=guard off`.

To inspect the current state without changing it: `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py" status`.

Report the printed status line back to the user. The enforcement is the hook's job — do not
police commands or paths yourself.
