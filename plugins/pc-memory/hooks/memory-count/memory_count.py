#!/usr/bin/env python3
"""PostToolUse hook: count, per memory note, in how many sessions it was consulted.

Runs after every successful `Read`. A read of a `*.md` file located directly in a memory
directory (the default silo of any project, or a directory declared through
`autoMemoryDirectory`, resolved as in `pc_memory_lib.py`, minus the repo's shared
`.claude/settings.json`, see below) is recorded in a ledger kept inside that
directory. `MEMORY.md` and any other file exit at once with no output.

The ledger, `.pc-memory-ledger.json`, is a dot file that is not a note and is separate from the
notes, so counting never rewrites the file that was read:

    {"notes": {"<note file name>": {"sessions": N, "last_session": "<id>",
                                    "last_read": "YYYY-MM-DD"}},
     "last_review": null}

A note's counter goes up by one only when the session differs from the note's `last_session`:
several reads in one session count once. `last_read` is the local date. Unknown keys and an
existing `last_review` survive a rewrite. A ledger that cannot be decoded or parsed is rebuilt from
the entries still usable, and the original is kept as `.pc-memory-ledger.json.corrupt` (an earlier
`.corrupt` is overwritten) before the new ledger is written. A ledger that cannot be read for
another reason (a directory, no permission) is left as it is and nothing is counted.

This hook writes into the directory, so it does not honour an `autoMemoryDirectory` declared in
the level's shared `.claude/settings.json`, which a cloned repo controls: only the level's
`settings.local.json`, the user settings and the default silos receive a ledger.

Two sessions counting together lose nothing: the read-modify-write holds an exclusive `flock` on
`.pc-memory-ledger.lock`, and the ledger is replaced atomically (temp file in the same directory,
then `os.replace`). The lock is polled without blocking and given up after `LOCK_TIMEOUT`
seconds: the count is then skipped, never the tool call. A file named
`.pc-memory-reconciling` in the memory directory, a regular file less than `MARKER_MAX_AGE` seconds
old, suspends the count for that directory; it is checked under the lock, so a writer that creates
the marker and then takes the lock cannot be overtaken by a count that started earlier. The note is
checked again under the lock: a note deleted while the count waited gets no entry back.

Never blocks and never fails the tool call: any error goes to stderr, nothing to stdout, exit 0.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pc_memory_lib import (  # noqa: E402
    HAS_FLOCK,
    INDEX_NAME,
    LEDGER_NAME,
    declared_memory_dirs,
    exclusive_lock,
    is_memory_dir,
    load_ledger,
    marker_active,
    write_ledger_atomically,
)

_UNRESOLVABLE = (OSError, ValueError, RuntimeError)


def note_location(payload: dict[str, object]) -> tuple[Path, str] | None:
    """Memory directory and note file name a successful `Read` payload consulted, else None.

    The cheap checks come first (tool, extension, `MEMORY.md` by name). Past them, a path inside a
    default silo needs no file access; any other `.md` path costs one settings lookup per level,
    which is what finds a declared `autoMemoryDirectory`.

    Args:
        payload: The decoded PostToolUse payload.

    Returns:
        The memory directory and the note's file name, or None when the read is not a note.
    """
    tool_input = payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if payload.get("tool_name") != "Read" or not isinstance(file_path, str):
        return None
    if not file_path.endswith(".md") or os.path.basename(file_path) == INDEX_NAME:
        return None
    raw_cwd = payload.get("cwd")
    try:
        cwd = Path(raw_cwd if isinstance(raw_cwd, str) and raw_cwd else os.getcwd())
        cwd = cwd.resolve()
        path = Path(os.path.normpath(cwd / os.path.expanduser(file_path)))
        declared = declared_memory_dirs(cwd)
        for variant in (path, path.resolve()):
            if is_memory_dir(variant.parent, declared):
                return variant.parent, variant.name
    except _UNRESOLVABLE:
        return None
    return None


def count_session(
    ledger: dict[str, object], note: str, session_id: str, today: date
) -> dict[str, object]:
    """Ledger with one consultation of `note` by `session_id` recorded.

    The counter goes up only when the session differs from the note's last one; `last_read` is
    always `today`.

    Args:
        ledger: A ledger as returned by `clean_ledger`; it is updated in place.
        note: The note's file name.
        session_id: The reading session.
        today: The local date to record.

    Returns:
        The same ledger.
    """
    notes = ledger["notes"]
    if not isinstance(notes, dict):
        notes = ledger["notes"] = {}
    entry = notes.get(note)
    if not isinstance(entry, dict):
        entry = {}
    if entry.get("last_session") != session_id:
        sessions = entry.get("sessions")
        entry["sessions"] = (sessions if isinstance(sessions, int) else 0) + 1
        entry["last_session"] = session_id
    entry["last_read"] = today.isoformat()
    notes[note] = entry
    return ledger


def record_read(payload: dict[str, object], today: date | None = None) -> bool:
    """Record the consultation a PostToolUse payload reports, when it is a memory note.

    Args:
        payload: The decoded PostToolUse payload.
        today: The date to record, the local date when omitted.

    Returns:
        True when the ledger was rewritten, False when nothing was counted.

    A lock still held after `LOCK_TIMEOUT` seconds raises `TimeoutError`, caught by `main`.
    """
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return False
    location = note_location(payload)
    if location is None:
        return False
    memory_dir, note = location
    if not HAS_FLOCK or not (memory_dir / note).is_file():
        return False
    ledger_file = memory_dir / LEDGER_NAME
    with exclusive_lock(memory_dir):
        if marker_active(memory_dir) or not (memory_dir / note).is_file():
            return False
        ledger = count_session(
            load_ledger(ledger_file), note, session_id, today or date.today()
        )
        write_ledger_atomically(ledger_file, ledger)
    return True


def main() -> int:
    """Entry point: read the payload on stdin, count the read, always exit 0 with no output."""
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record_read(payload)
    except Exception as exc:  # never fail the tool call
        sys.stderr.write(f"memory_count: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
