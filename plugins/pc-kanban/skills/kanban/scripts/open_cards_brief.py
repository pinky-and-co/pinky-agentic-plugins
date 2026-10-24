#!/usr/bin/env python3
"""The open cards of one chantier, each with its title and the opening of its goal — for the
"Flagging cards the closed card just made obsolete" step of `finishing-a-card.md`.

A title alone hides supersession: "Worker = an agent, not a chantier session" and "Explore
`/goal` + agent teams for card execution" share no word, yet the first delivered what
the second was after. The first block under a card's title — usually its why, or the note saying
what is left — is where that overlap shows, so it is printed next to each title.

Usage: open_cards_brief.py <chantier-dir>   (prints nothing when no card is open)

Python 3.12+, stdlib only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

OPEN_COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev")
GOAL_CHARS = 280

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


class CardBrief(NamedTuple):
    """One open card: its column, file name, title, and the opening of its goal."""

    column: str
    name: str
    title: str
    goal: str


def card_brief(path: Path) -> CardBrief:
    """Title and goal opening of one card.

    Args:
        path: the card's `.md` file.

    Returns:
        The card's brief; `goal` is the first text block after the `# ` title (quote marks and
        line breaks folded), cut to `GOAL_CHARS` with an ellipsis, or "" when there is none.
    """
    text = _FRONTMATTER_RE.sub("", path.read_text(encoding="utf-8", errors="ignore"))
    lines = text.splitlines()
    title, start = path.stem, 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title, start = line[2:].strip(), i + 1
            break
    block: list[str] = []
    for line in lines[start:]:
        stripped = line.lstrip("> ").strip()
        if line.startswith("#"):
            if block:
                break
            continue
        if not stripped:
            if block:
                break
            continue
        block.append(stripped)
    goal = " ".join(block)
    if len(goal) > GOAL_CHARS:
        goal = goal[: GOAL_CHARS - 1] + "…"
    return CardBrief(path.parent.name, path.name, title, goal)


def open_cards(chantier: Path) -> list[CardBrief]:
    """Every card of `OPEN_COLUMNS` in `chantier`, column order then file name.

    Args:
        chantier: the `CHANTIER{NNNN}-<slug>` directory.

    Returns:
        One brief per open card; empty when none is open.
    """
    return [
        card_brief(path)
        for column in OPEN_COLUMNS
        for path in sorted((chantier / column).glob("*.md"))
    ]


def render_open_cards(briefs: list[CardBrief]) -> str:
    """Two lines per card: `<column>/<file> — <title>`, then the indented goal.

    Args:
        briefs: what `open_cards` returned.

    Returns:
        The listing; "" for no card.
    """
    return "\n".join(
        f"{b.column}/{b.name} — {b.title}\n    {b.goal or '(no goal paragraph)'}"
        for b in briefs
    )


def main(argv: list[str] | None = None) -> int:
    """Print the open cards of the chantier given as the only argument."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: open_cards_brief.py <chantier-dir>", file=sys.stderr)
        return 2
    listing = render_open_cards(open_cards(Path(args[0])))
    if listing:
        print(listing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
