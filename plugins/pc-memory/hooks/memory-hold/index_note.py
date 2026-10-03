#!/usr/bin/env python3
"""Index a note the `memory-hold` mod moved to another level, in that level's `MEMORY.md`.

Usage: `index_note.py <memory_dir> <relative>`. Reads the note's frontmatter (`name`,
`description`) and appends `- [name](relative) — description` to the index, created when missing.
Nothing is added when a line of the index already links to the note. Prints `indexed` or
`already indexed`; exits 1 when the note cannot be read, 2 on a wrong call.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pc_memory_lib import (  # noqa: E402
    INDEX_NAME,
    clean_text,
    link_targets,
    split_frontmatter,
)


def build_index_line(relative: str, text: str) -> str:
    """The index line of a note: `- [name](relative) — description`, name falling back to the file.

    Args:
        relative: The note's path inside its memory directory.
        text: The note's content.

    Returns:
        One line, without its newline.
    """
    fields = split_frontmatter(text).fields
    name = " ".join(clean_text(fields.get("name", "")).split()) or Path(relative).stem
    description = " ".join(clean_text(fields.get("description", "")).split())
    line = f"- [{name}]({relative})"
    return f"{line} — {description}" if description else line


def index_note(memory_dir: Path, relative: str) -> bool:
    """Append the note's line to the index unless a line already links to it.

    Args:
        memory_dir: The level's memory directory.
        relative: The note's path inside it.

    Returns:
        True when a line was added, False when the note was already indexed.
    """
    text = (memory_dir / relative).read_text(encoding="utf-8")
    index = memory_dir / INDEX_NAME
    current = index.read_text(encoding="utf-8") if index.exists() else ""
    if any(relative in link_targets(line) for line in current.splitlines()):
        return False
    separator = "" if current == "" or current.endswith("\n") else "\n"
    with index.open("a", encoding="utf-8") as handle:
        handle.write(f"{separator}{build_index_line(relative, text)}\n")
    return True


def main() -> int:
    """Index the note named by the arguments.

    Returns:
        The process exit code.
    """
    if len(sys.argv) != 3:
        sys.stderr.write("usage: index_note.py <memory_dir> <relative>\n")
        return 2
    try:
        added = index_note(Path(sys.argv[1]), sys.argv[2])
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"index_note: {exc}\n")
        return 1
    print("indexed" if added else "already indexed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
