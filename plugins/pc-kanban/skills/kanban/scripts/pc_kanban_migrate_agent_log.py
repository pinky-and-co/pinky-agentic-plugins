#!/usr/bin/env python3
"""Nest a board's `_AGENT_LOG/` under its `_KANBAN/` root — replayable per board.

`agent_log_dir()` (`stop_handoff.py`) resolves `<kanban_root>/_AGENT_LOG`; a board whose journal
still sits next to `_KANBAN/` (the pre-nesting layout) needs its files moved there once. This
script does that move and leaves a relative symlink at the old sibling path, so a writer whose
plugin cache has not refreshed yet still lands its file in the same physical directory as a
writer already on the nested path — no content forked between the two.

Idempotent: rerunning on an already-migrated board (symlink already in place, or the board never
had a sibling `_AGENT_LOG/` at all) is a no-op, exit 0. Both paths present as real directories at
once is refused rather than merged — that state means something unexpected already happened and
a human should look, not a script guessing which side wins.

Python 3.11+, stdlib only.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def migrate(kanban: Path) -> str:
    """Move `kanban`'s sibling `_AGENT_LOG/` under `kanban/` and leave a compat symlink at the
    old path. Returns a one-line summary; raises `SystemExit` on a refusal.

    The rename and the symlink are two separate filesystem calls — if the process dies between
    them, `old` no longer exists but `new` already holds the content, with no symlink yet. A
    rerun in that state must not conclude "nothing to migrate": it recreates the symlink instead,
    so a rerun is always safe whether it landed before, between, or after the two calls."""
    kanban = kanban.resolve()
    if not kanban.is_dir():
        raise SystemExit(f"not a directory: {kanban}")
    old = kanban.parent / "_AGENT_LOG"
    new = kanban / "_AGENT_LOG"
    relative_target = os.path.relpath(new, old.parent)

    if old.is_symlink():
        target = old.resolve()
        if new.is_dir() and target == new.resolve():
            return f"already migrated — {old} is a compat symlink to {new}"
        raise SystemExit(
            f"refusing: {old} is a symlink to {target}, not the expected {new}"
        )
    if old.exists() and not old.is_dir():
        raise SystemExit(f"refusing: {old} exists and is not a directory")
    if new.exists() and not new.is_dir():
        raise SystemExit(f"refusing: {new} exists and is not a directory")
    if old.is_dir() and new.is_dir():
        raise SystemExit(
            f"refusing: both {old} and {new} exist as real directories — resolve by hand"
        )

    if old.is_dir():
        old.rename(new)
        old.symlink_to(relative_target, target_is_directory=True)
        return f"migrated {old} -> {new}, compat symlink left at {old} -> {relative_target}"
    if new.is_dir():
        old.symlink_to(relative_target, target_is_directory=True)
        return f"compat symlink recreated at {old} -> {relative_target} (content already at {new})"
    return f"nothing to migrate — no sibling {old}"


def main(argv: list[str]) -> int:
    """CLI entry point: `pc_kanban_migrate_agent_log.py --kanban <path-to-_KANBAN>`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kanban", required=True, type=Path, help="path to the board's _KANBAN/ root"
    )
    args = parser.parse_args(argv)
    print(migrate(args.kanban))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
