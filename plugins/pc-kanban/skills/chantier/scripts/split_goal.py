#!/usr/bin/env python3
"""Cut a /goal condition into one-line blocks the user can paste one after the other.

    split_goal.py < condition.txt

The CLI folds a paste longer than 800 characters into ``[Pasted text #N]``, which ``/goal`` does
not read as its condition. Each block stays under that bound, ends right after a ``; `` or a
space so the blocks concatenate to the condition, and is printed as a fenced block under a
``📋 Paste i/n`` title.

Exit status: 0 on success, 1 when the condition needs more than 5 blocks, 2 on empty input.
"""

from __future__ import annotations

import re
import sys

MAX_BLOCK = 790
MAX_BLOCKS = 5


def split_condition(text: str) -> list[str]:
    """Cut a condition into blocks of at most ``MAX_BLOCK`` characters.

    Args:
        text: The condition, on one line.

    Returns:
        The blocks, which concatenate back to ``text``.
    """
    pieces: list[str] = []
    for clause in re.split(r"(?<=; )", text):
        while len(clause) > MAX_BLOCK:
            cut = clause.rfind(" ", 0, MAX_BLOCK) + 1 or MAX_BLOCK
            pieces.append(clause[:cut])
            clause = clause[cut:]
        pieces.append(clause)
    blocks: list[str] = []
    for piece in pieces:
        if blocks and len(blocks[-1]) + len(piece) <= MAX_BLOCK:
            blocks[-1] += piece
        else:
            blocks.append(piece)
    return blocks


def render_blocks(blocks: list[str]) -> str:
    """Render the blocks as titled fenced blocks.

    Args:
        blocks: The pieces of the condition, in order.

    Returns:
        The text to relay to the user.
    """
    return "\n".join(
        f"📋 Paste {i}/{len(blocks)} ({len(b)} characters)\n```\n{b}\n```\n"
        for i, b in enumerate(blocks, 1)
    )


def main() -> int:
    """Run the command line.

    Returns:
        The exit status.
    """
    text = " ".join(sys.stdin.read().split())
    if not text:
        print("split_goal: empty condition on stdin", file=sys.stderr)
        return 2
    blocks = split_condition(text)
    if len(blocks) > MAX_BLOCKS:
        print(
            f"split_goal: {len(blocks)} blocs, maximum {MAX_BLOCKS} blocs — raccourcir la condition",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(render_blocks(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
