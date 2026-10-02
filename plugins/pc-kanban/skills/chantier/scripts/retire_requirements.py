#!/usr/bin/env python3
"""Retire from a chantier's story the requirements a card carried when it left the board.

    retire_requirements.py [--dry-run] <archived-card> <chantier-dir>

``archive_card.sh`` and ``drop_card.sh`` call it once the card sits in ``_ARCHIVE/``. The ids listed
under the card's frontmatter ``requirements`` key name the rows to remove from the table of
``<chantier-dir>/story.md``. A row stays when another open card of the same chantier still carries
its id: a requirement is not delivered until every card that traces to it is. The removed rows are
first copied verbatim into the archived card — the board is not versioned, that copy is the only way
back — then the story is rewritten atomically.

Exit status: 0 when done or nothing to do, 1 when a file could not be read or written (the message
says what is left to do).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

# Colonnes où une card est encore vivante : une exigence qu'elles portent n'est pas livrée.
OPEN_COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress")
ID_RE = re.compile(r"\b[A-Z]+-\d{4}-\d+\b")
KEY_RE = re.compile(r"^[A-Za-z_][\w-]*:")
SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|\s*$")


def frontmatter_requirements(text: str) -> list[str]:
    """Ids under the ``requirements`` key of a card's frontmatter, in order, without duplicates.

    Reads the inline form (``requirements: [a, b]``) and the multi-line list form, and nothing
    else: ids cited under another key (``blocked_by``, ``sert``) are not requirements.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return []
    block: list[str] = []
    collecting = False
    for line in lines[1:end]:
        if line.startswith("requirements:"):
            collecting = True
            block.append(line.partition(":")[2])
        elif collecting and KEY_RE.match(line):
            break  # clé suivante : la liste est finie
        elif collecting:
            block.append(line)
    return list(dict.fromkeys(ID_RE.findall("\n".join(block))))


def first_cell(line: str) -> str:
    """First cell of a markdown table row, or an empty string for any other line."""
    if not line.startswith("|"):
        return ""
    parts = line.split("|")
    return parts[1].strip() if len(parts) > 2 else ""


def table_header(lines: list[str]) -> list[str]:
    """Header and separator lines of the story's requirements table (empty when there is none)."""
    for i, line in enumerate(lines):
        if i > 0 and SEPARATOR_RE.match(line) and lines[i - 1].startswith("|"):
            return [lines[i - 1], line]
    return []


def open_card_carriers(chantier: Path) -> dict[str, str]:
    """Requirement id -> ``column/card.md`` of the first open card of the chantier carrying it."""
    carriers: dict[str, str] = {}
    for column in OPEN_COLUMNS:
        for card in sorted((chantier / column).glob("*.md")):
            text = card.read_text(encoding="utf-8", errors="ignore")
            for rid in frontmatter_requirements(text):
                carriers.setdefault(rid, f"{column}/{card.name}")
    return carriers


def still_cited(lines: list[str], rid: str) -> list[str]:
    """Labels of the remaining lines (post-removal) that still mention ``rid``."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(rid)}(?![\w-])")
    return [
        first_cell(line) or line.strip()[:40] for line in lines if pattern.search(line)
    ]


def write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` through a temporary file, so a failure never truncates it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def retire(card: Path, chantier: Path, dry_run: bool) -> int:
    """Retire the card's requirements from the chantier's story; see the module docstring."""
    card_text = card.read_text(encoding="utf-8")
    ids = frontmatter_requirements(card_text)
    if not ids:
        print("requirements: none listed in the card's frontmatter — nothing to retire")
        return 0
    story = chantier / "story.md"
    if not story.is_file():
        print(
            f"requirements: no story.md in {chantier} — nothing retired",
            file=sys.stderr,
        )
        return 0
    story_text = story.read_text(encoding="utf-8")
    lines = story_text.splitlines()
    carriers = open_card_carriers(chantier)

    rows: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        cell = first_cell(line)
        if cell in ids:
            rows.setdefault(cell, []).append(i)

    retired = [rid for rid in ids if rid in rows and rid not in carriers]
    messages = [
        f"requirements: kept {rid} (also carried by {carriers[rid]})"
        for rid in ids
        if rid in rows and rid in carriers
    ]
    absent = [rid for rid in ids if rid not in rows]
    if absent:
        messages.append(f"requirements: absent from story.md: {', '.join(absent)}")
    if not retired:
        print("\n".join(messages) or "requirements: nothing to retire")
        return 0

    remove = {i for rid in retired for i in rows[rid]}
    kept_lines = [line for i, line in enumerate(lines) if i not in remove]
    for rid in retired:
        cited = still_cited(kept_lines, rid)
        if cited:
            messages.append(
                f"requirements: {rid} is still cited by {', '.join(cited)} — left as is"
            )
    if dry_run:
        messages.insert(0, f"requirements: would retire {', '.join(retired)}")
        print("\n".join(messages))
        return 0

    # Copie d'abord : le board n'est pas versionné, la card archivée est la seule marche arrière.
    retired_lines = [lines[i] for i in sorted(remove)]
    if not all(
        line in card_text for line in retired_lines
    ):  # un second passage ne duplique pas
        prefix = "" if card_text.endswith("\n") else "\n"
        block = [
            "",
            f"## Requirements retired from the story ({date.today().isoformat()})",
            "",
            *table_header(lines),
            *retired_lines,
        ]
        with card.open("a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(block) + "\n")
    write_atomic(
        story, "\n".join(kept_lines) + ("\n" if story_text.endswith("\n") else "")
    )

    messages.insert(
        0, f"requirements: retired {', '.join(retired)} (rows copied into {card.name})"
    )
    messages.append(f"requirements: story.md now has {len(kept_lines)} lines")
    print("\n".join(messages))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retire from a chantier's story the requirements a card carried when it left the board.",
        epilog=(
            "examples:\n"
            "  retire_requirements.py <archived-card> <chantier-dir>  # retire the rows from story.md\n"
            "  retire_requirements.py --dry-run <card> <chantier-dir>  # report only, write nothing"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "card",
        type=Path,
        help="the card whose frontmatter `requirements` key names the rows; receives a copy of the "
        "retired rows (an archived card, or a 4-done card with --dry-run)",
    )
    parser.add_argument(
        "chantier",
        type=Path,
        help="the chantier directory, holding story.md and the card columns",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be retired and write nothing",
    )
    args = parser.parse_args(argv)
    try:
        return retire(args.card, args.chantier, args.dry_run)
    except (OSError, UnicodeDecodeError) as e:
        print(
            f"requirements: {e} — the card is archived, the story may be unchanged; "
            "re-run this command once fixed",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
