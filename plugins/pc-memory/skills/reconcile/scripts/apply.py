#!/usr/bin/env python3
"""Apply an approved reconcile plan to memory directories.

Usage: `apply.py --plan FILE|- [--dry-run] [--cwd PATH]`. `-` reads the plan from standard input.
Deterministic, no model.

The plan is a JSON object:

    {"actions": [{"dir": "<memory dir>", "note": "x.md", "action": "delete" | "move" | "keep",
                  "to_dir": "<memory dir>" (move only), "hard": false}],
     "reviewed_dirs": ["<memory dir>", ...]}

A directory is written `~/...` or absolute. The whole plan is validated before anything is
touched, and refused (exit 2, nothing changed) when:

- a `dir`, `to_dir` or reviewed directory is not the memory directory of the current level of
  `--cwd` or of one of its parents, as `inventory.py` lists them. A directory declared only in the
  shared project settings, which a cloned repo controls, is not one;
- a note name is not a plain `*.md` file name (separator, leading dot, `MEMORY.md` whatever its
  case), or names the index through a link or a case-insensitive file system, or is neither a
  regular file nor a symlink;
- two actions have the same source `(dir, note)`, or two moves the same destination;
- a `move` has no `to_dir`, goes onto itself or onto an existing file;
- a `.pc-memory-reconciling` marker younger than `MARKER_MAX_AGE` seconds is in an involved
  directory: another reconcile runs. An older one was left by a run that died: it is removed when
  the plan is applied.

Then, for every involved directory, the marker is created first (exclusively; it suspends the
consultation counter), the ledger lock is taken, and each action is applied in order, the marker
being refreshed before each one:

- `delete`: the note moves to `<dir>/.reconcile-trash/<YYYY-MM-DD>/` (removed for good only when
  `hard` is true), its index lines leave `<dir>/MEMORY.md`, its ledger entry is pruned;
- `move`: the index line is added to the destination index (a link to the note when the source had
  none), the file is copied to `to_dir` with an exclusive creation, so nothing is ever overwritten,
  and removed from the source, the line leaves the source index and the source ledger entry is
  pruned. A plan interrupted anywhere is applied again as it is: the note already at the
  destination is found and the indexes are repaired;
- `keep`: nothing.

An index line that links several notes is removed only when all of them are deleted or moved by the
plan; otherwise it stays and the result carries `shared_index_line`. Finally `last_review` (local
date) is set in the ledger of each reviewed directory. The markers are always removed, also when an
action fails and when the process gets SIGTERM or SIGHUP. A note already gone is reported
`already_gone` and is not an error.

Prints a JSON summary on stdout. Exit 0 when done, 1 when an action failed (`results` holds what was
done, the failed action with the steps it completed, `left` the actions not attempted), 2 when the
plan was refused. `--dry-run` prints the summary and changes nothing, not even the marker.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hooks"))
from pc_memory_lib import (  # noqa: E402
    INDEX_NAME,
    LEDGER_NAME,
    MARKER_NAME,
    clean_text,
    collect_levels,
    exclusive_lock,
    link_targets,
    load_ledger,
    marker_active,
    short,
    write_ledger_atomically,
)

TRASH_NAME = ".reconcile-trash"
LOCK_TIMEOUT = 10.0
ACTIONS = frozenset({"delete", "move", "keep"})
LINE_CHARS = 200
_UNRESOLVABLE = (OSError, ValueError, RuntimeError)
_DESCRIPTION = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


class PlanError(Exception):
    """The plan is refused: nothing was touched."""


@dataclass(frozen=True)
class Action:
    """One validated action of the plan."""

    directory: Path
    note: str
    action: str
    to_dir: Path | None
    hard: bool


@dataclass(frozen=True)
class Plan:
    """A validated plan."""

    actions: list[Action]
    reviewed: list[Path]

    def directories(self) -> list[Path]:
        """Every involved directory once, sorted by path."""
        found: set[Path] = set(self.reviewed)
        for item in self.actions:
            found.add(item.directory)
            if item.to_dir is not None:
                found.add(item.to_dir)
        return sorted(found)

    def removable(self, directory: Path) -> frozenset[str]:
        """Link targets, as an index may spell them, of the notes the plan takes out of `directory`."""
        names = {
            i.note
            for i in self.actions
            if i.directory == directory and i.action != "keep"
        }
        return frozenset(names | {quote(n) for n in names})


def _line(text: str) -> str:
    """`text` without control characters, capped, for a summary a model reads."""
    return clean_text(text)[:LINE_CHARS]


def _allowed_dirs(cwd: Path) -> list[Path]:
    """Memory directories a plan may name: the levels of `cwd` that `inventory.py` lists."""
    out: list[Path] = []
    for level in collect_levels(cwd, trust_shared_project_settings=False):
        out.extend((level.memory_dir, level.memory_dir.resolve()))
    return out


def _memory_dir(raw: object, allowed: list[Path], what: str) -> Path:
    """Absolute memory directory `raw` names, or a `PlanError`."""
    if not isinstance(raw, str) or not raw.strip():
        raise PlanError(f"{what}: a directory is required")
    try:
        path = Path(os.path.normpath(os.path.expanduser(raw.strip())))
        if not path.is_absolute():
            raise PlanError(f"{what}: {raw!r} is not an absolute path")
        if not path.is_dir():
            raise PlanError(f"{what}: {raw!r} is not a directory")
        if not any(v in allowed for v in (path, path.resolve())):
            raise PlanError(
                f"{what}: {raw!r} is not the memory directory of a level of the working directory"
            )
    except _UNRESOLVABLE as exc:
        raise PlanError(f"{what}: {raw!r} cannot be resolved: {exc}") from exc
    return path


def _note_name(raw: object, what: str) -> str:
    """Plain `*.md` file name, or a `PlanError`."""
    if not isinstance(raw, str) or not raw.endswith(".md") or raw.startswith("."):
        raise PlanError(f"{what}: {raw!r} is not a plain *.md note name")
    if raw.casefold() == INDEX_NAME.casefold() or raw != Path(raw).name:
        raise PlanError(f"{what}: {raw!r} is not a plain *.md note name")
    if "\\" in raw or "\0" in raw:
        raise PlanError(f"{what}: {raw!r} is not a plain *.md note name")
    return raw


def _check_note(directory: Path, note: str) -> None:
    """Refuse a note that is the index under another name, or neither a file nor a symlink.

    A note that does not exist is not an error: it is already gone.

    Args:
        directory: The memory directory.
        note: The note's file name.

    Raises:
        PlanError: When the entry is the index (same file, whatever the spelling) or another kind.
    """
    path = directory / note
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise PlanError(f"{short(path)} is neither a regular file nor a symlink")
    with contextlib.suppress(OSError):
        if os.path.samefile(path, directory / INDEX_NAME):
            raise PlanError(f"{short(path)} is the index, not a note")


def parse_plan(raw: object, cwd: Path) -> Plan:
    """Validate a decoded plan against the memory directories of `cwd`.

    Args:
        raw: The decoded JSON.
        cwd: Working directory the levels are resolved from.

    Returns:
        The validated plan.

    Raises:
        PlanError: When anything in the plan is not acceptable.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("actions"), list):
        raise PlanError("the plan must be an object with an `actions` list")
    allowed = _allowed_dirs(cwd)
    actions: list[Action] = []
    sources: set[tuple[Path, str]] = set()
    destinations: set[tuple[Path, str]] = set()
    for number, item in enumerate(raw["actions"], start=1):
        what = f"action {number}"
        if not isinstance(item, dict) or item.get("action") not in ACTIONS:
            raise PlanError(f"{what}: `action` must be one of {sorted(ACTIONS)}")
        directory = _memory_dir(item.get("dir"), allowed, f"{what} dir")
        note = _note_name(item.get("note"), f"{what} note")
        hard = item.get("hard", False)
        if not isinstance(hard, bool):
            raise PlanError(f"{what}: `hard` must be a boolean")
        source = (directory, note.casefold())
        if source in sources:
            raise PlanError(f"{what}: {note} is already the source of another action")
        sources.add(source)
        to_dir = None
        if item["action"] != "keep":
            _check_note(directory, note)
        if item["action"] == "move":
            to_dir = _memory_dir(item.get("to_dir"), allowed, f"{what} to_dir")
            if to_dir == directory:
                raise PlanError(f"{what}: `to_dir` is the source directory")
            destination = (to_dir, note.casefold())
            if destination in destinations:
                raise PlanError(
                    f"{what}: {note} is already the destination of another move"
                )
            destinations.add(destination)
            if os.path.lexists(directory / note) and os.path.lexists(to_dir / note):
                raise PlanError(f"{what}: {note} already exists in the destination")
        actions.append(Action(directory, note, item["action"], to_dir, hard))
    reviewed_raw = raw.get("reviewed_dirs", [])
    if not isinstance(reviewed_raw, list):
        raise PlanError("`reviewed_dirs` must be a list")
    reviewed = [_memory_dir(r, allowed, "reviewed_dirs") for r in reviewed_raw]
    plan = Plan(actions, reviewed)
    for directory in plan.directories():
        _check_marker(directory)
    return plan


def _check_marker(directory: Path) -> None:
    """Refuse a marker still active, or an entry of that name that is not a file.

    Args:
        directory: The memory directory.

    Raises:
        PlanError: When a reconcile seems to run in `directory`, or the marker cannot be removed.
    """
    path = directory / MARKER_NAME
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise PlanError(f"{short(path)} is not a regular file: remove it")
    if marker_active(directory):
        age = int(time.time() - info.st_mtime)
        raise PlanError(
            f"{short(directory)}: another reconcile runs (marker {age} s old); "
            "a marker older than 10 minutes is ignored"
        )


def _replace_text(path: Path, text: str) -> None:
    """Replace `path` with `text` through a temp file in the same directory."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pc-memory-index.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        with contextlib.suppress(OSError):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _index_text(directory: Path) -> str:
    """Text of the index of `directory`, empty when it has none."""
    try:
        return (directory / INDEX_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _linking_lines(directory: Path, note: str) -> list[str]:
    """Index lines of `directory` that link to `note`, without their line break."""
    return [
        line.rstrip("\r\n")
        for line in _index_text(directory).splitlines(keepends=True)
        if note in link_targets(line)
    ]


def _remove_index_lines(
    directory: Path, note: str, removable: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Remove from the index the lines that link to `note`, unless they link other notes too.

    A line is removed only when every one of its link targets is in `removable`.

    Args:
        directory: The memory directory.
        note: The note taken out.
        removable: Targets of the notes the plan takes out of `directory`.

    Returns:
        The removed lines and the lines kept because they link other notes.
    """
    index = directory / INDEX_NAME
    text = _index_text(directory)
    kept: list[str] = []
    removed: list[str] = []
    shared: list[str] = []
    for line in text.splitlines(keepends=True):
        targets = link_targets(line)
        if note in targets and all(t in removable for t in targets):
            removed.append(line.rstrip("\r\n"))
        else:
            kept.append(line)
            if note in targets:
                shared.append(line.rstrip("\r\n"))
    if removed:
        _replace_text(index, "".join(kept))
    return removed, shared


def _append_index_lines(directory: Path, note: str, lines: list[str]) -> bool:
    """Append `lines` to the index of `directory`, unless it already links to `note`.

    Args:
        directory: The memory directory.
        note: The note the lines link to.
        lines: The lines to append.

    Returns:
        True when the index was written.
    """
    text = _index_text(directory)
    if any(note in link_targets(line) for line in text.splitlines()):
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    _replace_text(directory / INDEX_NAME, text + "".join(f"{line}\n" for line in lines))
    return True


def _default_line(path: Path, note: str) -> str:
    """Index line for a note: a link, and its description when it has one."""
    head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    found = _DESCRIPTION.search(head)
    line = f"- [{Path(note).stem}]({note})"
    if found is None:
        return line
    description = " ".join(found.group(1).strip("\"'").split())
    return f"{line} - {description}"


def _prune_ledger(directory: Path, note: str) -> bool:
    """Drop `note` from the ledger of `directory`; True when an entry was there."""
    ledger_file = directory / LEDGER_NAME
    ledger = load_ledger(ledger_file, "apply")
    notes = ledger["notes"]
    if not isinstance(notes, dict) or note not in notes:
        return False
    del notes[note]
    write_ledger_atomically(ledger_file, ledger)
    return True


def _trash(directory: Path, note: str, today: date) -> Path:
    """Move `note` to the trash folder of `directory` and return its new path."""
    folder = directory / TRASH_NAME / today.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / note
    number = 0
    while os.path.lexists(target):
        number += 1
        target = folder / f"{note}.{number}"
    os.replace(directory / note, target)
    return target


def _move_exclusive(source: Path, dest: Path) -> None:
    """Move `source` to `dest` without ever replacing a file, across volumes too.

    The destination is created exclusively (a copy, or a symlink with the same target), then the
    source is removed. A copy that fails midway leaves no half file.

    Args:
        source: The file or symlink to move.
        dest: Where it goes; it must not exist.

    Raises:
        FileExistsError: When `dest` exists.
    """
    if source.is_symlink():
        os.symlink(os.readlink(source), dest)
    else:
        copied = False
        with open(source, "rb") as src, open(dest, "xb") as dst:
            try:
                shutil.copyfileobj(src, dst)
                copied = True
            finally:
                if not copied:
                    dst.close()
                    dest.unlink()
        with contextlib.suppress(OSError):
            shutil.copystat(source, dest)
    source.unlink()


def _destination_lines(item: Action, present: bool) -> list[str]:
    """Index lines the destination gets for a moved note.

    The source lines that link only this note, else a link made from the note's description.
    """
    assert item.to_dir is not None
    own = {item.note, quote(item.note)}
    lines = [
        ln
        for ln in _linking_lines(item.directory, item.note)
        if set(link_targets(ln)) <= own
    ]
    if lines:
        return lines
    where = item.directory if present else item.to_dir
    return [_default_line(where / item.note, item.note)]


def _apply_action(
    item: Action,
    today: date,
    removable: frozenset[str],
    result: dict[str, object],
    steps: list[str],
) -> None:
    """Apply one action, filling `result` and appending each completed step to `steps`."""
    if item.action == "keep":
        result["status"] = "kept"
        return
    source = item.directory / item.note
    present = os.path.lexists(source)
    if present:
        _check_note(item.directory, item.note)
    if item.action == "delete":
        result["hard"] = item.hard
        if present and item.hard:
            source.unlink()
            steps.append("file")
        elif present:
            result["trash"] = short(_trash(item.directory, item.note, today))
            steps.append("file")
    else:
        assert item.to_dir is not None
        dest = item.to_dir / item.note
        result["to_dir"] = short(item.to_dir)
        if present and os.path.lexists(dest):
            raise FileExistsError(f"{short(dest)} already exists")
        if present or os.path.lexists(dest):
            lines = _destination_lines(item, present)
            _append_index_lines(item.to_dir, item.note, lines)
            steps.append("destination_index")
        if present:
            _move_exclusive(source, dest)
            steps.append("file")
    removed, shared = _remove_index_lines(item.directory, item.note, removable)
    steps.append("source_index")
    result["status"] = "done" if present else "already_gone"
    result["index_lines_removed"] = len(removed)
    result["ledger_entry_pruned"] = _prune_ledger(item.directory, item.note)
    if shared:
        result["shared_index_line"] = [_line(s) for s in shared]


def _plan_result(item: Action) -> dict[str, object]:
    """What `--dry-run` reports for one action."""
    present = os.path.lexists(item.directory / item.note)
    status = "kept" if item.action == "keep" else "planned"
    return {
        "dir": short(item.directory),
        "note": item.note,
        "action": item.action,
        "status": status if present or item.action == "keep" else "already_gone",
    }


def _refresh(markers: list[Path]) -> None:
    """Touch the markers, so a long run is never taken for a dead one."""
    for marker in markers:
        os.utime(marker)


def _create_markers(plan: Plan, created: list[Path]) -> None:
    """Create the marker of each involved directory exclusively, noting each one in `created`.

    A marker left by a dead run is removed first. One that is still active makes the creation
    fail, and is not removed.
    """
    for directory in plan.directories():
        marker = directory / MARKER_NAME
        if os.path.lexists(marker) and not marker_active(directory):
            marker.unlink()
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        created.append(marker)


def apply_plan(plan: Plan, today: date, dry_run: bool = False) -> dict[str, object]:
    """Apply `plan`, markers first and removed last, and return the JSON summary.

    Args:
        plan: A plan validated by `parse_plan`.
        today: The local date for the trash folder and `last_review`.
        dry_run: Report what would happen and change nothing, not even the marker.

    Returns:
        The summary: `ok`, `results`, `last_review`, and on failure `error` and `left`.
    """
    summary: dict[str, object] = {"dry_run": dry_run, "ok": True}
    if dry_run:
        summary["results"] = [_plan_result(item) for item in plan.actions]
        summary["last_review"] = {short(d): today.isoformat() for d in plan.reviewed}
        return summary
    results: list[dict[str, object]] = []
    created: list[Path] = []
    try:
        _create_markers(plan, created)
        with contextlib.ExitStack() as stack:
            for directory in plan.directories():
                stack.enter_context(
                    exclusive_lock(
                        directory,
                        LOCK_TIMEOUT,
                        f"ledger lock of {short(directory)} still held",
                    )
                )
            for item in plan.actions:
                _refresh(created)
                result: dict[str, object] = {
                    "dir": short(item.directory),
                    "note": item.note,
                    "action": item.action,
                }
                steps: list[str] = []
                results.append(result)
                try:
                    _apply_action(
                        item, today, plan.removable(item.directory), result, steps
                    )
                except Exception:
                    result["status"] = "failed"
                    result["steps"] = steps
                    raise
            for directory in plan.reviewed:
                ledger_file = directory / LEDGER_NAME
                ledger = load_ledger(ledger_file, "apply")
                ledger["last_review"] = today.isoformat()
                write_ledger_atomically(ledger_file, ledger)
    except Exception as exc:
        summary["ok"] = False
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["left"] = [
            {"dir": short(i.directory), "note": i.note, "action": i.action}
            for i in plan.actions[len(results) :]
        ]
        summary["last_review"] = {}
    else:
        summary["last_review"] = {short(d): today.isoformat() for d in plan.reviewed}
    finally:
        for marker in created:
            with contextlib.suppress(FileNotFoundError):
                marker.unlink()
    summary["results"] = results
    return summary


def _exit_on_signal(signum: int, _frame: object) -> None:
    """Leave through `SystemExit`, so the `finally` of `apply_plan` removes the markers."""
    sys.exit(128 + signum)


def main() -> int:
    """Entry point: apply the plan, print the summary, exit 0, 1 (failed) or 2 (refused)."""
    parser = argparse.ArgumentParser(description="Apply an approved reconcile plan.")
    parser.add_argument(
        "--plan", required=True, help="JSON file holding the plan, - for stdin"
    )
    parser.add_argument("--dry-run", action="store_true", help="change nothing")
    parser.add_argument("--cwd", default=None, help="directory the levels start from")
    args = parser.parse_args()
    cwd = Path(args.cwd if args.cwd else os.getcwd()).expanduser().resolve()
    try:
        text = (
            sys.stdin.read()
            if args.plan == "-"
            else Path(args.plan).read_text(encoding="utf-8")
        )
        plan = parse_plan(json.loads(text), cwd)
    except (OSError, ValueError, PlanError) as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}, ensure_ascii=False))
        return 2
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _exit_on_signal)
    summary = apply_plan(plan, date.today(), args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
