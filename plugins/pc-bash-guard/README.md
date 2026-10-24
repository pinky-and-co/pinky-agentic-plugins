# pc-bash-guard

Asks before a broadly allowlisted Bash command (`find -delete`/`-exec`, `echo` redirected to a file) does something destructive.

## Overview

The plugin ships a single PreToolUse hook. `find` and `echo` are commonly allowlisted wholesale so their read-only uses never prompt, but their destructive forms change state. The hook detects those forms and sends the call back to a permission prompt in every permission mode except `auto`, where the classifier already judges each call. It fails open: a malformed payload or an unexpected error leaves the command untouched.

## Skills

None — this plugin ships only a hook.

## Hooks

- **PreToolUse → Bash** — runs `hooks/dangerous_bash_ask.py` on every Bash call (5 s timeout). The script scans the command line for a destructive form of a broadly allowlisted command: `find` with `-delete`, `-exec`, `-execdir`, `-ok`, `-okdir`, `-fprint*` or `-fls` (called directly or through the `rtk` wrapper), and `echo` redirected to a file with `>` or `>>`. On a match it returns `permissionDecision: "ask"` with a reason naming the form and the permission mode the hook saw. In `auto` mode it lets the call pass.

## Requirements

- `python3` — the hook script is stdlib-only Python (3.11+).

## Dependencies

None.
