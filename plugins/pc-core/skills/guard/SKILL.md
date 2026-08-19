---
name: guard
description: Toggle both guards at once — careful (confirm destructive Bash) + freeze (lock edits to a scope)
model: haiku
allowed-tools: Bash(python3:*), Bash(py:*)
---

# /guard — careful + freeze together

Convenience over `/careful` + `/freeze`: turns **both** guards on (or off) in one call. Use it when
you start focused work on a module and want the full safety envelope — confirm before destructive
commands AND lock edits to that module.

## Action

`$ARGUMENTS`:
- `/guard <dir>...` → careful **on** + freeze locked to those directories.
- `/guard on` → careful **on** (freeze unchanged).
- `/guard off` → both guards cleared.
- `/guard` with no args → careful **on** (freeze unchanged).

Run the control script (use `py -3` instead of `python3` on Windows):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py" guard $ARGUMENTS
```

Report the printed status line. To inspect the current state without changing it, run the same
script with `status`. Enforcement is the PreToolUse hook's job — do not police commands or paths
yourself.
