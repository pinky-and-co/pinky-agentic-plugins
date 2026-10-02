#!/usr/bin/env python3
"""Inventory of the memory notes of the current level and of its parent levels, as JSON.

Usage: `inventory.py [--cwd PATH]`. Deterministic, no model, and it never writes anything: not a
ledger, not a marker, not a corrupt ledger's rename.

Levels are the ones the SessionStart hook shows (`pc_memory_lib.collect_levels`): the current
level and each ancestor level that has a `MEMORY.md`, nearest first. A directory declared only in
the shared project settings, which a cloned repo controls, is not listed: `apply.py` would refuse
it. Per level:

    {"level": "~/ws/repo", "dir": "~/.claude/projects/-ws-repo/memory", "role": "current",
     "last_review": null | "YYYY-MM-DD",
     "index": {"lines": N, "bytes": N, "over_budget": bool},
     "notes": [{"file", "description", "type", "age_days", "sessions", "last_read",
                "in_index", "index_line"}],
     "orphans": {"index_without_file": [...], "file_without_index": [...]}}

`duplicates.exact` lists the groups of notes, across every level shown, whose body after the
frontmatter is the same once whitespace is normalised. A note with an empty body is not compared.

`index_line` is the first index line linking the note, null when none does. Every text read from a note, an index or the
ledger (descriptions, types, index lines, file names, orphan and duplicate names) has its control
characters removed and is capped, so it is safe to show to a model; `last_read` and `last_review`
are kept only when they are `YYYY-MM-DD` dates, else null.
Dot files (ledger, lock, `.corrupt`, temp files, the trash folder) and `MEMORY.md` are not notes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hooks"))
from pc_memory_lib import (  # noqa: E402
    INDEX_NAME,
    LEDGER_NAME,
    Level,
    clean_ledger,
    clean_text,
    collect_levels,
    level_root,
    link_targets,
    note_age_days,
    short,
)

INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024
TEXT_CHARS = 200
FRONTMATTER_LIMIT = 100
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Flat `key: value` pairs of a leading `---` block, and the body that follows it."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1 : FRONTMATTER_LIMIT + 1], start=1):
        if line.strip() == "---":
            return fields, "\n".join(lines[index + 1 :])
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            fields[key.strip()] = value.strip().strip("\"'")
    return {}, text


def _text(value: str) -> str:
    """`value` without control characters, on one line, capped."""
    return " ".join(clean_text(value).split())[:TEXT_CHARS]


def _clean(value: str) -> str:
    """`value` without control characters, capped: a name or a path fragment, spaces kept."""
    return clean_text(value)[:TEXT_CHARS]


def _date(value: object) -> str | None:
    """`value` when it is a `YYYY-MM-DD` date string, else None."""
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return value
    return None


def _read_ledger(directory: Path) -> dict[str, object]:
    """Ledger of a memory directory, empty when absent or unusable. Never renames or writes."""
    try:
        return clean_ledger(
            json.loads((directory / LEDGER_NAME).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return clean_ledger(None)


def _note_files(directory: Path) -> list[Path]:
    """Regular `*.md` files of a memory directory that are notes, sorted by name."""
    out: list[Path] = []
    for path in sorted(directory.glob("*.md")):
        if path.name == INDEX_NAME or path.name.startswith("."):
            continue
        if path.is_file():
            out.append(path)
    return out


def _index_report(directory: Path) -> tuple[dict[str, object], list[str]]:
    """Size report of the index and its lines."""
    raw = (directory / INDEX_NAME).read_bytes()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    report: dict[str, object] = {
        "lines": len(lines),
        "bytes": len(raw),
        "over_budget": len(lines) > INDEX_MAX_LINES or len(raw) > INDEX_MAX_BYTES,
    }
    return report, lines


def _level_report(
    level: Level, current: Path, now: float
) -> tuple[dict[str, object], list[tuple[str, str]]]:
    """Report of one level, and the (file, normalised body) pairs of its notes."""
    directory = level.memory_dir
    ledger = _read_ledger(directory)
    stored = ledger["notes"] if isinstance(ledger["notes"], dict) else {}
    index, lines = _index_report(directory)
    line_of: dict[str, str] = {}
    linked: set[str] = set()
    for line in lines:
        for target in link_targets(line):
            linked.add(target)
            line_of.setdefault(target, _text(line))
    notes: list[dict[str, object]] = []
    bodies: list[tuple[str, str]] = []
    names: set[str] = set()
    for path in _note_files(directory):
        text = path.read_text(encoding="utf-8", errors="replace")
        fields, body = _split_frontmatter(text)
        entry = stored.get(path.name)
        counted = entry if isinstance(entry, dict) else {}
        sessions = counted.get("sessions")
        shown = _clean(path.name)
        names.add(path.name)
        bodies.append((shown, " ".join(body.split())))
        notes.append(
            {
                "file": shown,
                "description": _text(fields.get("description", "")),
                "type": _text(fields.get("type", "")),
                "age_days": note_age_days(path, now),
                "sessions": sessions if isinstance(sessions, int) else 0,
                "last_read": _date(counted.get("last_read")),
                "in_index": path.name in linked,
                "index_line": line_of.get(path.name),
            }
        )
    missing = sorted(
        _clean(t)
        for t in linked
        if t.endswith(".md")
        and t.casefold() != INDEX_NAME.casefold()
        and not (directory / t).is_file()
        and unquote(t) not in linked - {t}
    )
    report = {
        "level": short(level.root),
        "dir": short(directory),
        "role": "current" if level.root == current else "parent",
        "last_review": _date(ledger["last_review"]),
        "index": index,
        "notes": notes,
        "orphans": {
            "index_without_file": missing,
            "file_without_index": sorted(_clean(n) for n in names if n not in linked),
        },
    }
    return report, bodies


def build_inventory(cwd: Path, now: float) -> dict[str, object]:
    """Inventory of the levels of `cwd`: per-level reports and the exact duplicates.

    A level that cannot be read is left out with a message on stderr, the others are kept.

    Args:
        cwd: Working directory the levels are resolved from.
        now: Timestamp the note ages are computed against.

    Returns:
        The JSON-ready inventory.
    """
    current = level_root(cwd)
    levels: list[dict[str, object]] = []
    groups: dict[str, list[dict[str, str]]] = {}
    for level in collect_levels(cwd, trust_shared_project_settings=False):
        try:
            report, bodies = _level_report(level, current, now)
        except OSError as exc:
            sys.stderr.write(f"inventory: {short(level.memory_dir)}: {exc}\n")
            continue
        levels.append(report)
        for name, body in bodies:
            if body:
                groups.setdefault(body, []).append(
                    {"level": short(level.root), "file": name}
                )
    exact = [sorted(g, key=lambda m: (m["level"], m["file"])) for g in groups.values()]
    return {
        "levels": levels,
        "duplicates": {
            "exact": sorted(
                (group for group in exact if len(group) > 1),
                key=lambda g: (g[0]["level"], g[0]["file"]),
            )
        },
    }


def main() -> int:
    """Entry point: print the inventory of `--cwd` (default: the working directory)."""
    parser = argparse.ArgumentParser(description="Inventory of memory notes, as JSON.")
    parser.add_argument("--cwd", default=None, help="directory the levels start from")
    args = parser.parse_args()
    cwd = Path(args.cwd if args.cwd else os.getcwd()).expanduser().resolve()
    print(json.dumps(build_inventory(cwd, time.time()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
