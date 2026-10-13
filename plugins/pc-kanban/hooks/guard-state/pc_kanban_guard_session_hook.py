#!/usr/bin/env python3
"""DirectoryAdded / SessionEnd hook — keep the permission guard's per-session state in step.

- ``DirectoryAdded`` (a `/add-dir` typed in a session): append the directory to that session's
  ``added_dirs`` — earlier additions kept, no duplicate. This is how `guard.py`'s outside-scope
  guard learns about it: no hook input and no settings file carries a typed `/add-dir`.
- ``SessionEnd``: drop that session's entry; the other sessions' entries stay.

The state file layout and its rules live in `pc_kanban_guard_state.py`. The hook emits no
decision on stdout and spawns nothing. An event it cannot use (no usable `session_id`, or a
`DirectoryAdded` with no usable `directory`) prints one line on stderr naming the event's keys and
exits 1 — a non-blocking failure, neither event has decision control. Any other failure exits 0:
a state bug must never break a session.

Python 3.11+, stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_PLUGIN / "skills" / "permission-change" / "scripts"))
import guard  # noqa: E402  — seulement pour `project_root`, l'unique endroit qui résout la racine
import pc_kanban_guard_state as gs  # noqa: E402


def _absolute(directory: str, event: dict) -> str:
    """The added directory as an absolute, normalized path (relative → against the event cwd)."""
    path = Path(directory).expanduser()
    if not path.is_absolute():
        path = Path(event.get("cwd") or os.getcwd()) / path
    return os.path.normpath(path)


def _unusable(event: dict, what: str) -> bool:
    """Report an event the hook cannot use on stderr; always returns False (not applied)."""
    print(
        f"guard session hook: {event.get('hook_event_name')} without a usable {what}, nothing "
        f"recorded — event keys: {sorted(event)}",
        file=sys.stderr,
    )
    return False


def apply(event: dict) -> bool:
    """Update the state file for one `DirectoryAdded` or `SessionEnd` event.

    Args:
        event: The hook event JSON.

    Returns:
        False when the event lacks what it needs (reported on stderr), True otherwise.
    """
    session_id = event.get("session_id")
    directory = event.get("directory")
    name = event.get("hook_event_name")
    if not isinstance(session_id, str) or not session_id:
        session_id = None
    if not isinstance(directory, str) or not directory:
        directory = None
    if name == "DirectoryAdded":
        if session_id is None or directory is None:
            fields = (("session_id", session_id), ("directory", directory))
            return _unusable(event, " and ".join(n for n, v in fields if v is None))
        added = _absolute(directory, event)

        def append(entry: dict) -> None:
            if added not in entry["added_dirs"]:
                entry["added_dirs"].append(added)

        gs.update_session(gs.state_path(guard.project_root(event)), session_id, append)
    elif name == "SessionEnd":
        if session_id is None:
            return _unusable(event, "session_id")
        gs.drop_session(gs.state_path(guard.project_root(event)), session_id)
    return True


def main() -> int:
    """Read the event from stdin and apply it. Exit 1 for an unusable event, 0 otherwise."""
    try:
        return 0 if apply(json.loads(sys.stdin.read() or "{}")) else 1
    except Exception as exc:  # un hook ne casse jamais une session sur son propre bug
        print(f"guard session hook: skipped ({exc!r})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
