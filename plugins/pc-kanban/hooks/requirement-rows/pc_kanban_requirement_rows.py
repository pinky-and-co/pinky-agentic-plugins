#!/usr/bin/env python3
"""Parse the requirement rows of a chantier's open cards, for every skill that reads them.

A requirement is *defined* by a row of the ``## Requirements`` table of a card and *cited* by the
``requirements:`` key of a card's frontmatter. The same id may be defined in several open cards
(``0-ideas`` to ``3-in-progress``) provided the rows are identical on priority, statement and check.
Shared by the ``chantier`` skill (``story_view.py``) and the ``spawn-workers`` skill, so neither
imports the other's scripts. Python 3.11+, stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

OPEN_COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress")
ID_RE = re.compile(r"\b([A-Z]+)-(\d{4})-(\d+)\b")
KEY_RE = re.compile(r"^[A-Za-z_][\w-]*:")


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


def read_open_cards(chantier: Path) -> list[tuple[str, str]]:
    """Read the open cards of a chantier in column then name order.

    Args:
        chantier: The chantier directory.

    Returns:
        ``(slug, text)`` pairs; an unreadable card is skipped.
    """
    cards = []
    for column in OPEN_COLUMNS:
        for card in sorted((chantier / column).glob("*.md")):
            try:
                text = card.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                # Une card illisible (lien cassé…) ne doit faire échouer aucun appelant.
                continue
            cards.append((card.stem, text))
    return cards


@dataclass(frozen=True)
class RequirementRow:
    """One row of a card's ``## Requirements`` table.

    Equality is the ISO test: two rows are equal when id, priority, statement and check match;
    the defining card and the verbatim row do not take part.

    Attributes:
        id: The requirement id.
        priority: The priority cell.
        statement: The statement cell.
        check: The check cell, extra trailing cells joined with `` | ``.
        slug: The card that defines the row.
        row: The verbatim table row.
    """

    id: str
    priority: str
    statement: str
    check: str
    slug: str = field(compare=False)
    row: str = field(compare=False)


def requirement_rows(cards: list[tuple[str, str]]) -> list[RequirementRow]:
    """Collect the defining rows of every card, in board order.

    Args:
        cards: ``(slug, text)`` pairs in board order.

    Returns:
        One row per ``(card, id)``, cards in order, ids in table order.
    """
    rows: list[RequirementRow] = []
    for slug, text in cards:
        for rid, row in defined_rows(text).items():
            cells = [c.strip() for c in row.strip().strip("|").split("|")][1:]
            cells += [""] * (3 - len(cells))
            while len(cells) > 3 and not cells[-1]:
                cells.pop()
            rows.append(
                RequirementRow(
                    rid, cells[0], cells[1], " | ".join(cells[2:]), slug, row
                )
            )
    return rows
