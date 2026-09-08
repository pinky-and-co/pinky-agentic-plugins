#!/usr/bin/env python3
"""pending_gate — PreToolUse hook (matcher: *).

Turns the agent's next tool call into a permission `ask` whenever
pending_user_action has queued something only the user can do (a slash
command the CLI parses before a turn). The `ask` carries every queued
message, so the request is a hard stop instead of a notification that
scrolls away while the agent keeps working.

`surface()` empties the queue, so this fires once per batch — approve to
continue, the next tool call is not gated again. A source that still needs
the action re-queues it on its next trigger.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pending_user_action import surface


def run(session_id: str) -> dict:
    """The hook's decision for `session_id`: `{}` when the queue is empty,
    otherwise a PreToolUse `ask` carrying every queued message. `surface`
    empties the queue, so this returns the payload once per batch."""
    messages = surface(session_id)
    if not messages:
        return {}

    reason = (
        "⏸️ Action requise avant de continuer :\n"
        + "\n".join(f"  • {m}" for m in messages)
        + "\n\nFais-la, puis approuve — ou approuve pour continuer sans."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def main_with_payload(payload: dict) -> None:
    print(json.dumps(run(payload["session_id"])))


def main() -> None:
    # Fail open, always. This is a matcher-less PreToolUse hook — a raised
    # exception here would block *every* tool call. A bad payload, a broken
    # import, a missing queue file: none of that is worth locking the session.
    try:
        main_with_payload(json.load(sys.stdin))
    except Exception:
        print("{}")


if __name__ == "__main__":
    main()
