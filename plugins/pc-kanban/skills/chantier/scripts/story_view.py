#!/usr/bin/env python3
"""Rebuild a chantier's story view on demand from its open cards.

    story_view.py <chantier-dir>
    story_view.py --next-id <FAMILY> <chantier-dir>

Open cards are the ones in ``0-ideas`` to ``3-in-progress``. A requirement is *defined* in the
``## Requirements`` table of exactly one card and *cited* by other cards through their frontmatter
``requirements:`` key. The view prints the requirements defined in open cards, the open questions of
open cards, the open cards ordered so a card follows the one that blocks it, and a warning for each
id cited by an open card but defined in no open card.

``--next-id PKG`` prints the next free id of the family for the chantier (``PKG-0025-13``), counting
open cards, the chantier's cards under ``_ARCHIVE/`` and ``story.md``.

Exit status: 0 on success, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OPEN_COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress")
ID_RE = re.compile(r"\b([A-Z]+)-(\d{4})-(\d+)\b")
KEY_RE = re.compile(r"^[A-Za-z_][\w-]*:")
EMPTY = ("", "null", "~", "[]")


def frontmatter(text: str) -> list[str]:
    """Return the lines between the opening and closing ``---`` of a card.

    Args:
        text: The whole card.

    Returns:
        The frontmatter lines, empty when there is none.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return []


def cited_ids(head: list[str]) -> list[str]:
    """Return the ids under the ``requirements`` frontmatter key (inline or list form).

    Args:
        head: The frontmatter lines.

    Returns:
        The ids in order, without duplicates.
    """
    ids: list[str] = []
    inside = False
    for line in head:
        if line.startswith("requirements:"):
            inside = True
        elif KEY_RE.match(line):
            inside = False
        if inside:
            ids += [m.group(0) for m in ID_RE.finditer(line)]
    return list(dict.fromkeys(ids))


def blocker(head: list[str]) -> str:
    """Return the ``blocked_by`` slug of a card, or an empty string.

    Args:
        head: The frontmatter lines.

    Returns:
        The slug without quotes.
    """
    for line in head:
        if line.startswith("blocked_by:"):
            value = line.partition(":")[2].strip().strip("'\"")
            return "" if value in EMPTY else value
    return ""


def section(text: str, title: str) -> list[str]:
    """Return the body lines of a ``## <title>`` section, up to the next ``## `` heading.

    Args:
        text: The whole card.
        title: The heading text.

    Returns:
        The body lines, empty when the section is absent.
    """
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == title
        elif inside:
            body.append(line)
    return body


def defined_rows(text: str) -> dict[str, str]:
    """Return the requirement rows of a card's ``## Requirements`` table, by id.

    Args:
        text: The whole card.

    Returns:
        Id mapped to the verbatim table row.
    """
    rows: dict[str, str] = {}
    for line in section(text, "Requirements"):
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) > 2 and ID_RE.fullmatch(cells[1].strip()):
            rows.setdefault(cells[1].strip(), line.rstrip())
    return rows


def open_questions(text: str) -> list[str]:
    """Return the items of a card's ``## Open questions`` section, one string each.

    Args:
        text: The whole card.

    Returns:
        Each bullet (continuation lines joined) or paragraph.
    """
    items: list[str] = []
    for line in section(text, "Open questions"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
        elif items:
            items[-1] += " " + stripped
        else:
            items.append(stripped)
    return items


def read_open_cards(chantier: Path) -> list[tuple[str, str]]:
    """Read the open cards of a chantier in column then name order.

    Args:
        chantier: The chantier directory.

    Returns:
        ``(slug, text)`` pairs.
    """
    cards = []
    for column in OPEN_COLUMNS:
        for card in sorted((chantier / column).glob("*.md")):
            cards.append((card.stem, card.read_text(encoding="utf-8", errors="ignore")))
    return cards


def order_cards(cards: list[tuple[str, str]]) -> list[str]:
    """Order card slugs so a card comes after the open card that blocks it.

    Args:
        cards: ``(slug, text)`` pairs in board order.

    Returns:
        The slugs, blockers first; a blocking cycle falls back to board order.
    """
    slugs = [s for s, _ in cards]
    blockers = {s: blocker(frontmatter(t)) for s, t in cards}
    ordered: list[str] = []
    pending = list(slugs)
    while pending:
        ready = [
            s for s in pending if blockers[s] not in slugs or blockers[s] in ordered
        ]
        if not ready:
            ordered += pending
            break
        ordered.append(ready[0])
        pending.remove(ready[0])
    return ordered


def archived_texts(chantier: Path) -> list[str]:
    """Read the chantier's archived cards (done and dropped).

    Args:
        chantier: The chantier directory.

    Returns:
        The card texts.
    """
    number = chantier.name.split("-", 1)[0]
    root = chantier.parent / "_ARCHIVE"
    return [
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(root.glob(f"{number}-*/*.md"))
    ]


def next_id(chantier: Path, family: str) -> str:
    """Compute the next free id of a family for the chantier.

    Args:
        chantier: The chantier directory.
        family: The family prefix, e.g. ``PKG``.

    Returns:
        The id, e.g. ``PKG-0025-13``.
    """
    number = chantier.name.split("-", 1)[0].removeprefix("CHANTIER")
    texts = [t for _, t in read_open_cards(chantier)] + archived_texts(chantier)
    story = chantier / "story.md"
    if story.is_file():
        texts.append(story.read_text(encoding="utf-8", errors="ignore"))
    highest = 0
    for text in texts:
        for m in ID_RE.finditer(text):
            if m.group(1) == family and m.group(2) == number:
                highest = max(highest, int(m.group(3)))
    return f"{family}-{number}-{highest + 1}"


def render(chantier: Path) -> str:
    """Build the story view of a chantier.

    Args:
        chantier: The chantier directory.

    Returns:
        The text to print.
    """
    cards = read_open_cards(chantier)
    out = ["## Requirements (open cards)", ""]
    defined: set[str] = set()
    story = chantier / "story.md"
    # Une story pas encore migrée garde sa table : ses lignes restent valides jusqu'au déplacement.
    sources = (
        [("story.md", story.read_text(encoding="utf-8", errors="ignore"))]
        if story.is_file()
        else []
    )
    for slug, text in sources + cards:
        for rid, row in defined_rows(text).items():
            defined.add(rid)
            out.append(f"{row}  <- {slug}")
    out += ["", "## Open questions", ""]
    out += [f"- [{slug}] {q}" for slug, text in cards for q in open_questions(text)]
    out += ["", "## Open cards (blockers first)", ""]
    out += [f"- {slug}" for slug in order_cards(cards)]
    unresolved = [
        f"{rid} cited by {slug}"
        for slug, text in cards
        for rid in cited_ids(frontmatter(text))
        if rid not in defined
    ]
    if unresolved:
        out += ["", "## Warning: cited but defined in no open card", ""]
        out += [f"- {u}" for u in unresolved]
    return "\n".join(out) + "\n"


def main() -> int:
    """Run the command line.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("chantier", type=Path, help="chantier directory")
    parser.add_argument("--next-id", metavar="FAMILY", help="print the next free id")
    args = parser.parse_args()
    if not args.chantier.is_dir():
        print(f"no such chantier directory: {args.chantier}", file=sys.stderr)
        return 2
    chantier = args.chantier.resolve()
    if args.next_id is not None:
        if not re.fullmatch(r"[A-Z]+", args.next_id):
            print(f"not a requirement family: {args.next_id!r}", file=sys.stderr)
            return 2
        print(next_id(chantier, args.next_id))
    else:
        sys.stdout.write(render(chantier))
    return 0


if __name__ == "__main__":
    sys.exit(main())
