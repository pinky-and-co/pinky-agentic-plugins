---
name: freeze
description: Lock edits to one or more directories — the PreToolUse guard denies writes outside the frozen scope
model: haiku
allowed-tools: Bash(python3:*), Bash(py:*)
---

# /freeze — lock edits to a directory

Restricts where files may be written. While a freeze is active, the PreToolUse hook (`guard.py`)
denies any `Edit` / `Write` / `MultiEdit` / `NotebookEdit` whose target sits **outside** the frozen
directories (`permissionDecision: deny`). Unlike `/careful`, this is a hard lock, not a prompt — it
is the guard rail for "only touch this module while I work on it".

**Honest limit**: the lock covers the write-class TOOLS only. A Bash write (`sed -i`, `tee`, `>`
redirect, `mv`/`cp`) is NOT intercepted — and `/careful`'s destructive catalogue does not cover
those either. Freeze protects against the agent's ordinary edit path, not against shell writes
(no command-parsing whack-a-mole — a hard guarantee belongs to `permissions.deny` on the paths).

## Action

`$ARGUMENTS` is one or more directories (the writable scope), or `off` to lift the lock.

Run the control script (use `py -3` instead of `python3` on Windows):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py" freeze $ARGUMENTS
```

- `/freeze src/core` → edits are locked to `src/core/` and below.
- `/freeze src/core tests` → both directories are writable, nothing else.
- `/freeze off` → lock lifted.

Report the printed status line back to the user. The enforcement is the hook's job — do not police
paths yourself.
