---
name: deny
description: Block edits to one or more directories, leaving everything else open — the PreToolUse guard denies writes inside the denied scope
model: haiku
allowed-tools: Bash(python3:*), Bash(py:*)
---

# /deny — block a directory, leave everything else open

The inverse of `/freeze`. While a deny list is active, the PreToolUse hook (`guard.py`) denies any
`Edit` / `Write` / `MultiEdit` / `NotebookEdit` whose target sits **inside** a denied directory
(`permissionDecision: deny`) — everywhere else stays writable. Use it for "don't touch THIS while
I work on everything around it", the opposite guard rail from `/freeze`'s "only touch this".

**Not the same thing as `permissions.deny`** (a `settings.json` key): that one is enforced by the
harness itself and survives even a hook rewrite, but takes effect only at session start. This skill
is a session-scoped toggle, effective on the very next turn — reach for `permissions.deny` instead
when you need the harder guarantee.

**Honest limit**: the lock covers the write-class TOOLS only. A Bash write (`sed -i`, `tee`, `>`
redirect, `mv`/`cp`) is NOT intercepted — same boundary as `/freeze` and `/careful`.

## Action

`$ARGUMENTS` is one or more directories (the forbidden scope), or `off` to lift it.

Run the control script (use `py -3` instead of `python3` on Windows):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py" --mode=deny $ARGUMENTS
```

- `/deny secrets/` → `secrets/` becomes unwritable, everything else stays open.
- `/deny secrets/ .env` → both denied, nothing else affected.
- `/deny off` → the deny list is cleared.

Report the printed status line back to the user. The enforcement is the hook's job — do not police
paths yourself.
