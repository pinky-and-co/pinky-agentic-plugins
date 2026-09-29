#!/usr/bin/env python3
"""CLI behind the `agents-md` skill — seed or standalone-render a repo's AGENTS.md.

Usage:
    python3 agents_md_repo.py [--standalone] [--workspace <path>] [--] <repo-path>

Default: create `<repo>/AGENTS.md` only when absent (folding `CONTEXT.md`'s glossary in, then
deleting `CONTEXT.md` once the glossary is confirmed in the written file); an existing file, even
a dangling symlink, is never touched. `--standalone`: atomically overwrite with the full
standalone render (Gotchas + Behavior policy + the hand-authored body); a symlinked AGENTS.md is
refused. The repo must have a `.git` entry; the workspace must be an existing directory that is
not itself a repo. No symlink is ever created.

Python 3.12+, stdlib only.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agents_md_lib import (  # noqa: E402
    _context_md_glossary,
    _repo_default_body,
    atomic_write,
    find_workspace_root,
    is_valid_workspace_root,
    render_standalone_agents_md,
)


def seed_repo_agents_md(repo: Path) -> Path | None:
    """Seed `<repo>/AGENTS.md` once, folding in then deleting `CONTEXT.md`.

    The file is created exclusively (never truncating anything), and `CONTEXT.md` is removed only
    once its glossary text is confirmed in the written file.

    Args:
        repo: The repo directory.

    Returns:
        The created path, or None when an AGENTS.md already exists.

    Raises:
        BaseException: A failed write, after the half-written file is removed.
    """
    agents_path = repo / "AGENTS.md"
    if os.path.lexists(agents_path):
        return None
    glossary = _context_md_glossary(repo)
    content = f"# AGENTS.md — {repo.name}\n\n{_repo_default_body(repo)}\n"
    try:
        with open(agents_path, "x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        return None
    except BaseException:
        agents_path.unlink(missing_ok=True)
        raise
    context_path = repo / "CONTEXT.md"
    if (
        glossary
        and context_path.is_file()
        and glossary in agents_path.read_text(encoding="utf-8")
    ):
        context_path.unlink()
    return agents_path


def write_standalone_agents_md(repo: Path, workspace_root: Path) -> Path:
    """Atomically overwrite `<repo>/AGENTS.md` with the standalone render.

    Only the hand-authored body survives.

    Args:
        repo: The repo directory.
        workspace_root: The workspace whose profile feeds Gotchas and Behavior.

    Returns:
        The written path.
    """
    agents_path = repo / "AGENTS.md"
    existing = (
        agents_path.read_text(encoding="utf-8") if agents_path.is_file() else None
    )
    atomic_write(
        agents_path, render_standalone_agents_md(repo, workspace_root, existing)
    )
    return agents_path


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir() or not os.path.lexists(repo / ".git"):
        return _fail(f"not a git repo: {repo}")

    if not args.standalone:
        created = seed_repo_agents_md(repo)
        if created is None:
            print(f"exists, left untouched {repo / 'AGENTS.md'}")
        else:
            print(f"created {created}")
        return 0

    if args.workspace is not None:
        given = Path(args.workspace).resolve()
        if not is_valid_workspace_root(given):
            return _fail(f"invalid workspace (missing, a repo, or $HOME): {given}")
        workspace: Path | None = given
    else:
        workspace = find_workspace_root(repo, os.environ)
    if workspace is None:
        return _fail(
            "workspace not resolvable — pass --workspace or set PINKY_WORKSPACE_ROOT"
        )
    if (repo / "AGENTS.md").is_symlink():
        return _fail(
            f"AGENTS.md is a symlink, refusing to overwrite: {repo / 'AGENTS.md'}"
        )
    path = write_standalone_agents_md(repo, workspace)
    print(f"wrote standalone {path}")
    return 0


def main(argv: list[str]) -> int:
    """Run the CLI; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (OSError, ValueError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
