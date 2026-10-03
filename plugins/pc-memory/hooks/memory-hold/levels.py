#!/usr/bin/env python3
"""Memory levels a held note can be written to, for the `memory-hold` mod.

Usage: `levels.py <cwd> <file_path>`. Prints one JSON object on stdout:
`{"levels": [{"name", "dir", "current", "target"}], "relative": <path in its level | null>}`.
The levels are the ones `memory_write_guard.py` lists to Claude (`writable_levels`), nearest first;
`target` marks the level `file_path` is headed to and `relative` is its path inside that level's
memory directory, null when it is headed to none of them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pc_memory_lib import writable_levels  # noqa: E402


def _relative(path: Path, directory: Path) -> str | None:
    """Path of `path` inside `directory`, with or without symlinks resolved; None when outside."""
    for candidate in {path, path.resolve()}:
        for base in {directory, directory.resolve()}:
            if base in candidate.parents:
                return candidate.relative_to(base).as_posix()
    return None


def levels_for(cwd: Path, file_path: Path) -> dict[str, object]:
    """The levels offered for a note, and where the note sits in the one it is headed to.

    Args:
        cwd: The session's working directory.
        file_path: The absolute path the note is being written to.

    Returns:
        The `levels` list and the note's `relative` path, as printed on stdout.
    """
    path = Path(os.path.normpath(file_path))
    levels: list[dict[str, object]] = []
    relative = None
    for index, level in enumerate(writable_levels(cwd)):
        inside = _relative(path, level.memory_dir) if relative is None else None
        relative = relative or inside
        levels.append(
            {
                "name": level.root.name or str(level.root),
                "dir": str(level.memory_dir),
                "current": index == 0,
                "target": inside is not None,
            }
        )
    return {"levels": levels, "relative": relative}


def main() -> int:
    """Print the levels for the arguments; exit 2 on a wrong call.

    Returns:
        The process exit code.
    """
    if len(sys.argv) != 3:
        sys.stderr.write("usage: levels.py <cwd> <file_path>\n")
        return 2
    print(json.dumps(levels_for(Path(sys.argv[1]), Path(sys.argv[2]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
