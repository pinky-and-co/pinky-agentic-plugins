#!/usr/bin/env python3
"""Per-session state of the permission guard — one JSON file, one entry per Claude Code session.

``<root>/.claude/.guard-state.json`` (override: ``PC_GUARD_STATE``)::

    {"sessions": {"<session_id>": {"careful": false, "freeze": [], "deny": [],
                                   "added_dirs": [], "updated": "YYYY-MM-DD HH:MM"}}}

``careful``, ``freeze`` and ``deny`` are a session's own settings; ``added_dirs`` are the
directories a `/add-dir` typed in that session added. A session never reads another session's
entry. A sub-agent's tool calls carry its parent's ``session_id`` (observed: a parent's `deny`
blocked a sub-agent's `Write`), so a sub-agent shares its parent's entry.

Lifecycle: the `SessionEnd` hook drops the session's entry; every write also drops entries not
modified for seven days, `SessionEnd` not being guaranteed to run.

A file that is absent, unreadable, not JSON or not in this shape reads as "no entry" — never an
error, so a guard decision always falls back to "nothing added, nothing frozen".

Every writer takes an exclusive `flock` on the state directory around its read-modify-write
and replaces the file atomically. When `fcntl` is missing or the lock is refused, the writer
runs unlocked and says so on stderr (`HAS_FLOCK` is False without `fcntl`).

A file still in the flat, pre-session format is renamed to `<path>.legacy` by the first write
(kept if a `.legacy` already exists) and read as empty. Python 3.11+, stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable, Generator
from datetime import datetime, timedelta
from pathlib import Path

try:
    import fcntl
except ImportError:  # win32 : pas de verrou consultatif, le remplacement atomique tient
    fcntl = None  # type: ignore[assignment]

STATE_ENV = "PC_GUARD_STATE"
DEFAULT_STATE_REL = ".claude/.guard-state.json"
MAX_AGE = timedelta(days=7)
UPDATED_FORMAT = "%Y-%m-%d %H:%M"
LIST_KEYS = ("freeze", "deny", "added_dirs")
HAS_FLOCK = fcntl is not None


def state_path(root: Path) -> Path:
    """Where the guard state lives — env override, else `<root>/.claude/.guard-state.json`."""
    override = os.environ.get(STATE_ENV)
    return Path(override) if override else root / DEFAULT_STATE_REL


def _clean(entry: object) -> dict:
    """A session entry reduced to its known keys, each with the right type."""
    raw = entry if isinstance(entry, dict) else {}
    clean: dict = {"careful": bool(raw.get("careful"))}
    for key in LIST_KEYS:
        values = raw.get(key)
        clean[key] = [str(v) for v in values] if isinstance(values, list) else []
    return clean


def _read(path: Path) -> dict[str, dict]:
    """The raw `sessions` table, or an empty one for anything that is not the current format."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return {str(sid): e for sid, e in sessions.items() if isinstance(e, dict)}


def load_session(path: Path, session_id: object) -> dict:
    """The state of one session — `careful`, `freeze`, `deny`, `added_dirs`; empty by default."""
    sid = session_id if isinstance(session_id, str) and session_id else None
    return _clean(_read(path).get(sid) if sid else None)


def _fresh(entry: dict, now: datetime) -> bool:
    """True when the entry was modified within `MAX_AGE`; a missing/garbled stamp is stale."""
    try:
        stamp = datetime.strptime(str(entry.get("updated")), UPDATED_FORMAT)
    except ValueError:
        return False
    return now - stamp <= MAX_AGE


@contextlib.contextmanager
def _locked(path: Path) -> Generator[None, None, None]:
    """Hold an exclusive advisory lock on the state directory (no sidecar file to ignore)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    if fcntl is None:
        print("guard state: fcntl unavailable, writing without a lock", file=sys.stderr)
    else:
        try:
            fd = os.open(path.parent, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            fd = None
            print(
                f"guard state: lock not acquired ({exc}), writing without it",
                file=sys.stderr,
            )
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _is_legacy(path: Path) -> bool:
    """True when the file is a JSON object with no `sessions` table (the flat, older format)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "sessions" not in data


def _write(path: Path, sessions: dict[str, dict], now: datetime) -> None:
    """Replace the file with `sessions` minus the stale entries.

    A flat-format file is first renamed to `<path>.legacy`, unless one already exists.
    """
    kept = {sid: e for sid, e in sessions.items() if _fresh(e, now)}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps({"sessions": kept}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        legacy = path.with_name(f"{path.name}.legacy")
        if _is_legacy(path) and not legacy.exists():
            os.replace(path, legacy)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def update_session(path: Path, session_id: str, mutate: Callable[[dict], None]) -> dict:
    """Read the session's entry, let `mutate` change it in place, stamp and persist it.

    The read, the mutation and the write happen under the lock, so a concurrent writer's change
    to another key of the same entry survives. Returns the entry as written.
    """
    with _locked(path):
        sessions = _read(path)
        entry = _clean(sessions.get(session_id))
        mutate(entry)
        now = datetime.now()
        entry = _clean(entry)
        sessions[session_id] = {**entry, "updated": now.strftime(UPDATED_FORMAT)}
        _write(path, sessions, now)
    return entry


def drop_session(path: Path, session_id: str) -> None:
    """Remove the session's entry, leaving every other session's untouched."""
    if not path.exists():
        return
    with _locked(path):
        sessions = _read(path)
        if session_id in sessions:
            del sessions[session_id]
            _write(path, sessions, datetime.now())
