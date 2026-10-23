#!/usr/bin/env python3
"""Build the upload zip of the brainstorm skill for claude.ai.

    build_zip.py [--out <zip-path>]

The zip holds ``brainstorm/SKILL.md``, copied byte for byte from the source. It is written to the
system temporary directory by default, so the working tree stays clean; ``--out`` must name a
path outside the git repository that holds this script.

Exit status: 0 on success, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "SKILL.md"
ENTRY = "brainstorm/SKILL.md"
FIXED_DATE = (2020, 1, 1, 0, 0, 0)


def repo_root(start: Path) -> Path | None:
    """Return the git work tree containing ``start``, or None.

    Args:
        start: A path inside the candidate repository.

    Returns:
        The directory holding ``.git``, or None when there is none.
    """
    for parent in (start, *start.parents):
        if (parent / ".git").exists():
            return parent
    return None


def build(out: Path) -> Path:
    """Write the zip.

    Args:
        out: Destination path of the zip.

    Returns:
        The path written.
    """
    info = zipfile.ZipInfo(ENTRY, date_time=FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(info, SKILL.read_bytes())
    return out


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments, defaulting to ``sys.argv``.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="zip path, outside the git repository")
    args = parser.parse_args(argv)
    out: Path = args.out or Path(tempfile.gettempdir()) / "brainstorm-skill.zip"
    out = out.resolve()
    root = repo_root(SKILL.parent)
    if root is not None and out.is_relative_to(root):
        print(f"refusing to write inside the repository: {out}", file=sys.stderr)
        return 2
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        print(build(out))
    except OSError as error:
        print(f"cannot write {out}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
