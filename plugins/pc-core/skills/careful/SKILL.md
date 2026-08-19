---
name: careful
description: Toggle careful mode — the PreToolUse guard asks for confirmation before destructive Bash commands
model: haiku
allowed-tools: Bash(python3:*), Bash(py:*)
---

# /careful — confirm before destructive commands

Turns the **careful** guard on or off. While on, the PreToolUse hook (`guard.py`) intercepts
destructive `Bash` commands (`rm -rf`, `git push --force`, `git reset --hard`, `DROP`, `TRUNCATE`,
`DELETE FROM` without `WHERE`, `sudo`, …) and returns `permissionDecision: ask` — Claude Code prompts
you to confirm before running. It is a speed bump, not a block: one keystroke clears each prompt.

## Action

`$ARGUMENTS` is `on` (default), `off`, or empty.

Run the control script (use `py -3` instead of `python3` on Windows):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py" --mode=careful $ARGUMENTS
```

Then report the printed status line (e.g. `careful: on · freeze: off`) back to the user. Do not do
anything else — the enforcement is the hook's job, not yours.
