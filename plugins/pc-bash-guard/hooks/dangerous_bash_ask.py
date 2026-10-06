#!/usr/bin/env python3
"""PreToolUse hook — ask before a destructive form of an allowlisted Bash command.

`find` and `echo` are allowlisted wholesale so their read-only uses never prompt, but
`find -delete`, `find -exec` and `echo > file` change state. This hook sends those forms back to
a prompt in every permission mode except `auto`, where the classifier already judges each call.
The reason shown in the prompt names the mode the hook saw.

Python 3.11+, stdlib only. A hook failure must never block a session → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

AUTO_MODE = "auto"

_FIND_ACTION = re.compile(
    r"(?:^|[\s;&|(])(?:rtk\s+)?find\b[^;&|\n]*?\s-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)\b"
)
_ECHO_REDIRECT = re.compile(r"(?:^|[\s;&|(])echo\b[^;&|\n]*?(?<![0-9&>])>{1,2}(?!&)")


def dangerous_form(command: str) -> str | None:
    """Name the destructive form a Bash command contains, if any.

    Args:
        command: The Bash command line about to run.

    Returns:
        A short label for the destructive form, or None when the command has none.
    """
    if _FIND_ACTION.search(command):
        return "find with a deleting or executing action"
    if _ECHO_REDIRECT.search(command):
        return "echo redirected to a file"
    return None


def decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build the hook response for one PreToolUse payload.

    Args:
        payload: The decoded PreToolUse JSON from Claude Code.

    Returns:
        The hook response asking for a prompt, or None when the command should pass untouched.
    """
    mode = payload.get("permission_mode")
    if mode == AUTO_MODE:
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    form = dangerous_form(command)
    if form is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"pc-bash-guard: {form} (permission mode: {mode or 'unknown'})",
        }
    }


def main() -> int:
    """Read the payload from stdin and print the hook response, if any.

    Returns:
        The process exit code — always 0, so a hook failure never blocks a session.
    """
    try:
        response = decide(json.load(sys.stdin))
    except (ValueError, OSError):
        return 0
    if response is not None:
        sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())
