---
name: permission-change
description: Toggle the PreToolUse permission guards — careful (confirm destructive Bash), freeze (lock edits to a scope), deny (block edits to a scope), or guard (careful + freeze together). Use when the user asks to change or check a permission guard.
argument-hint: "careful|freeze|guard|deny [scope-dir...] [on|off] | status"
arguments: [mode]
model: haiku
allowed-tools: Bash(python3:*), Bash(py:*)
---

# /permission-change — one entry point for the permission guards

Four related guards, one command — `$mode` (the first word) picks which. All four are enforced by
the same PreToolUse hook (`guard.py`) and share one state file; `$mode` decides what gets written
to it.

**Why `arguments: [mode]` and not `[mode, scope]` or more**: `$mode` is always the first token,
unambiguous whatever follows. Everything after it is variadic and mode-dependent (0+ scope
directories for `freeze`/`deny`/`guard`, or a bare `on`/`off` for `careful`) — a fixed named slot
for it would mislabel real invocations, e.g. `/permission-change freeze src/core tests` has TWO
scope directories; a `$scope` bound to position 1 would silently drop `tests` and a `$status`
bound to position 2 would call it a status when it is really the second directory. `$ARGUMENTS`
(the full string) stays the only correct vehicle for that variadic remainder — see Action below.

## Modes

- **`careful [on|off]`** — while on, a destructive `Bash` command (`rm -rf`,
  `git push --force`, `git reset --hard`, `DROP`, `TRUNCATE`, `DELETE FROM` without `WHERE`,
  `sudo`, …) returns `permissionDecision: ask` instead of running silently. A speed bump, not
  a block.
- **`freeze <dir>... | off`** — a whitelist: locks `Edit`/`Write`/`MultiEdit`/
  `NotebookEdit` to the given directories; anything outside is denied.
- **`deny <dir>... | off`** — the inverse of `freeze`: a blocklist. The given
  directories become unwritable; everything else stays open. Not the same as the
  `permissions.deny` settings key (harder guarantee, session-start only, not a toggle).
- **`guard <dir>... | on | off`** — convenience: `careful` on + `freeze` to the given
  directories in one call.

**Honest limit, all four**: only the write-class TOOLS are covered. A Bash write (`sed -i`,
`tee`, `>`, `mv`/`cp`) is NOT intercepted — for a hard guarantee, use `permissions.deny` instead.

## Action

`$ARGUMENTS` is the mode word plus whatever that mode takes — pass it straight through, do not
reinterpret it.

Run the control script (use `py -3` instead of `python3` on Windows):

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/guard.py" $ARGUMENTS
```

Examples: `/permission-change careful on`, `/permission-change freeze src/core`,
`/permission-change deny secrets/`, `/permission-change guard off`.

To inspect the current state without changing it: `python3 "${CLAUDE_SKILL_DIR}/scripts/guard.py" status`.

Report the printed status line back to the user. The enforcement is the hook's job — do not
police commands or paths yourself.
