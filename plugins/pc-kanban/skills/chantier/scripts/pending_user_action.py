#!/usr/bin/env python3
"""pending_user_action — a session-scoped queue of actions only the user can do.

Some things a hook or a skill needs done can only be done by the user: a
slash command the CLI parses *before* a turn (`/reload-plugins`, `/rename`,
`/goal`), so no hook and no skill body can run it. The old channel for these
was an ephemeral notification or a line at the tail of a response — both
scroll away unread while the agent keeps working.

This queue is the replacement. A source calls `require(key, message)`; the
`pending_gate` PreToolUse hook reads the queue and turns the agent's next
tool call into a permission `ask` carrying every pending message, so the
request is a hard stop the user cannot miss. `surface()` returns the
messages and empties the queue — the gate calls it once, so each `require`
nags once, not on every tool call; a source that still needs the action
calls `require` again on its next trigger.

Library: `require`/`resolve`/`surface`/`pending`/`clear_all`, all taking an
explicit `session_id`. CLI (for skill bodies, which cannot import it):

    pending_user_action.py require <key> <message>
    pending_user_action.py resolve <key>
    pending_user_action.py pending          # one 'key\\tmessage' per line

The CLI reads the session id from CLAUDE_CODE_SESSION_ID.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "pinky-plugin-scope"


def _queue_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.pending.json"


def _load(session_id: str) -> dict[str, str]:
    try:
        data = json.loads(_queue_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(session_id: str, queue: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _queue_path(session_id).write_text(json.dumps(queue))


def require(session_id: str, key: str, message: str) -> None:
    """Add (or replace) the pending action under `key`. Re-calling with the
    same key just refreshes the message — the queue never holds duplicates."""
    queue = _load(session_id)
    queue[key] = message
    _save(session_id, queue)


def resolve(session_id: str, key: str) -> None:
    """Drop the pending action under `key` if present — a no-op otherwise."""
    queue = _load(session_id)
    if queue.pop(key, None) is not None:
        _save(session_id, queue)


def pending(session_id: str) -> dict[str, str]:
    """The current queue, `{key: message}`, without touching it."""
    return _load(session_id)


def surface(session_id: str) -> list[str]:
    """Return every pending message (queue order) and empty the queue. The
    gate calls this exactly once per batch, so a `require` surfaces once."""
    queue = _load(session_id)
    if not queue:
        return []
    messages = list(queue.values())
    _save(session_id, {})
    return messages


def clear_all(session_id: str) -> None:
    """Drop the whole queue — SessionEnd, so a crashed session leaves none."""
    try:
        _queue_path(session_id).unlink()
    except FileNotFoundError:
        pass


def _cli(argv: list[str]) -> int:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        print("CLAUDE_CODE_SESSION_ID is not set", file=sys.stderr)
        return 2
    match argv:
        case ["require", key, message]:
            require(session_id, key, message)
        case ["resolve", key]:
            resolve(session_id, key)
        case ["pending"]:
            for key, message in pending(session_id).items():
                print(f"{key}\t{message}")
        case _:
            print(
                "usage: pending_user_action.py require <key> <message> | "
                "resolve <key> | pending",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
