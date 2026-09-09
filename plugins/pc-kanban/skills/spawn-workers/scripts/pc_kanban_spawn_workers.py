#!/usr/bin/env python3
"""pc_kanban_spawn_workers.py — one cmux workspace per repo of a kanban card.

The orchestrator session (opened outside any git repo, board curation only) runs this to hand a
selected card to worker sessions: one `cmux new-workspace` per entry of the card's `repos:`
frontmatter, each scoped (`--cwd`) to the local clone, its workspace (`--name`) and Claude session
(`claude -n`) both named `<repo>-<slug>`, booting straight into `/pc-kanban:chantier <id> <slug>`.

No dependency on the `pinky` CLI (FR-0025-20): card lookup, board resolution and `repos:` parsing
all go through `stop_handoff.py`'s CLI (same plugin, same pattern guard.py uses); slug->path
resolution reuses `resolve_repos.sh` (sibling skill). The workspace root is derived from the board
that holds the card — never a configured value.

Multi-repo cards (FR-0025-21) are sequential by convention: pass the single next `<repo-slug>` to
spawn just that one. With no slug filter every repo of the card is spawned, in `repos:` order.

`--dry-run` prints the exact `cmux` command line(s) without spawning anything — the only side-effect
-free way to demonstrate the resolution from a worker session.

Python 3.11+, stdlib only.

Usage: pc_kanban_spawn_workers.py [--dry-run] <chantier-id> <card-slug> [<repo-slug>...]
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

_USAGE = "Usage: pc_kanban_spawn_workers.py [--dry-run] <chantier-id> <card-slug> [<repo-slug>...]"

# `slug` and `chantier_id` end up inside the shell text cmux types into a fresh terminal
# (`--command`) and inside a `glob()` pattern — constrain both to what a real card name uses so
# neither a metacharacter nor a quote can ride through.
_ID_RE = re.compile(r"[0-9]{3,4}")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_HOOK = Path(__file__).resolve().parents[3] / "hooks" / "stop_handoff.py"
_RESOLVE_REPOS = (
    Path(__file__).resolve().parents[2] / "chantier" / "scripts" / "resolve_repos.sh"
)


class SpawnTarget(NamedTuple):
    """A repo slug paired with the local clone path resolved for it."""

    slug: str
    path: Path


def _hook(*args: str) -> str:
    """Run a `stop_handoff.py` CLI mode and return its stdout, aborting on a non-zero exit.

    Args:
        args: the mode flag and its operands, e.g. ("--card-path", "0025", "my-slug").

    Returns:
        The command's stdout, stripped of the trailing newline.

    Raises:
        SystemExit: the hook exited non-zero (its stderr is forwarded).
    """
    proc = subprocess.run(
        [sys.executable, str(_HOOK), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"spawn-workers: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def board_workspace_root(card_path: Path) -> Path:
    """The workspace root a card's clones live under — the parent of its `_KANBAN` ancestor.

    Args:
        card_path: absolute path to the card file, under `<root>/_KANBAN/CHANTIER*/...`.

    Returns:
        The `<root>` directory.

    Raises:
        SystemExit: the path has no `_KANBAN` ancestor.
    """
    for parent in card_path.parents:
        if parent.name == "_KANBAN":
            return parent.parent
    raise SystemExit(f"spawn-workers: {card_path} is not under a _KANBAN board")


def select_repos(declared: list[str], wanted: list[str]) -> list[str]:
    """Filter a card's declared repos to those asked for, preserving declared order.

    Args:
        declared: the card's `repos:` slugs, in frontmatter order.
        wanted: repo slugs passed on the command line; empty means all declared.

    Returns:
        The declared slugs to spawn, in declared order.

    Raises:
        SystemExit: a wanted slug is not one of the card's declared repos.
    """
    if not wanted:
        return declared
    unknown = [w for w in wanted if w not in declared]
    if unknown:
        raise SystemExit(
            f"spawn-workers: {unknown} not in the card's repos: {declared}"
        )
    return [d for d in declared if d in wanted]


def resolve_paths(workspace_root: Path, slugs: list[str]) -> list[SpawnTarget]:
    """Resolve each org/repo slug to its local clone path via resolve_repos.sh.

    Args:
        workspace_root: root the bounded `.git` scan walks from.
        slugs: org/repo slugs to resolve, in order.

    Returns:
        One SpawnTarget per slug, in the same order.

    Raises:
        SystemExit: resolve_repos.sh reported 0 or >1 clones for a slug.
    """
    proc = subprocess.run(
        ["bash", str(_RESOLVE_REPOS), str(workspace_root), *slugs],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"spawn-workers: {proc.stderr.strip()}")
    paths = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(paths) != len(slugs):
        raise SystemExit(
            f"spawn-workers: resolve_repos.sh returned {len(paths)} paths for {len(slugs)} slugs"
        )
    return [SpawnTarget(slug, Path(p)) for slug, p in zip(slugs, paths, strict=True)]


def build_spawn_command(target: SpawnTarget, chantier_id: str, slug: str) -> list[str]:
    """Build the `cmux new-workspace` argv for one repo of a card.

    Args:
        target: the repo slug and its resolved local path.
        chantier_id: the four-digit chantier number.
        slug: the card filename without ".md".

    Returns:
        The full argv list, ready for subprocess.run without a shell.
    """
    name = f"{target.slug.split('/')[-1]}-{slug}"
    inner = f'claude -n "{name}" "/pc-kanban:chantier {chantier_id} {slug}"'
    return [
        "cmux",
        "new-workspace",
        "--cwd",
        str(target.path),
        "--name",
        name,
        "--command",
        inner,
    ]


def main(argv: list[str]) -> int:
    """Parse args, resolve the card and its repos, then spawn (or print) one workspace each.

    Args:
        argv: command-line arguments without the program name.

    Returns:
        Process exit code.

    Raises:
        SystemExit: the id/slug pair is malformed, the card is missing or ambiguous, a repo
            slug is not one the card declares, resolve_repos.sh failed, or the card declares
            no repos at all.
    """
    dry_run = "--dry-run" in argv
    positional = [a for a in argv if a != "--dry-run"]
    if len(positional) < 2:
        print(_USAGE, file=sys.stderr)
        return 2
    chantier_id, slug, *wanted = positional
    if not _ID_RE.fullmatch(chantier_id) or not _SLUG_RE.fullmatch(slug):
        raise SystemExit(
            f"spawn-workers: '{chantier_id} {slug}' is not a <digits> <card-slug> pair"
        )

    card_path = Path(_hook("--card-path", chantier_id, slug))
    declared = [s for s in _hook("--card-repos", str(card_path)).splitlines() if s]
    if not declared:
        raise SystemExit(
            f"spawn-workers: card '{slug}' declares no repos: — nothing to spawn"
        )

    slugs = select_repos(declared, wanted)
    targets = resolve_paths(board_workspace_root(card_path), slugs)

    for target in targets:
        command = build_spawn_command(target, chantier_id, slug)
        if dry_run:
            print(shlex.join(command))
            continue
        subprocess.run(command, check=True)
        print(f"spawned {target.slug.split('/')[-1]}-{slug} → {target.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
