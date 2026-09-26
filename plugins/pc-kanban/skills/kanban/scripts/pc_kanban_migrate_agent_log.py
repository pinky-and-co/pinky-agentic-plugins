#!/usr/bin/env python3
"""Nest a board's `_AGENT_LOG/` under its `_KANBAN/` root — replayable per board.

`agent_log_dir()` (`stop_handoff.py`) resolves `<kanban_root>/_AGENT_LOG`; a board whose journal
still sits next to `_KANBAN/` (the pre-nesting layout) needs its files moved there once. This
script does that move — no compat is kept at the old path: a writer whose plugin cache still
hardcodes the sibling path creates a fresh, disjoint directory there instead of landing in the
same place as a reader using the nested resolution. Accepted: coordinating every writer's cache
refresh is out of this script's reach.

Idempotent: rerunning on an already-migrated board (no sibling left, or the board never had one
at all) is a no-op, exit 0. Both paths present as real directories at once is refused rather than
merged — that state means something unexpected already happened and a human should look, not a
script guessing which side wins.

Python 3.11+, stdlib only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def migrate(kanban: Path) -> str:
    """Move `kanban`'s sibling `_AGENT_LOG/` under `kanban/`. Returns a one-line summary; raises
    `SystemExit` on a refusal.

    A leftover symlink at the old path (from a board migrated before compat was dropped) is left
    untouched — cleaning it up is a separate, explicit decision, not this script's job."""
    kanban = kanban.resolve()
    if not kanban.is_dir():
        raise SystemExit(f"not a directory: {kanban}")
    old = kanban.parent / "_AGENT_LOG"
    new = kanban / "_AGENT_LOG"

    if old.is_symlink():
        return (
            f"nothing to migrate — {old} is a leftover compat symlink, left untouched"
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
        return f"migrated {old} -> {new}"
    if new.is_dir():
        return f"already migrated — {new} exists, no sibling {old}"
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
