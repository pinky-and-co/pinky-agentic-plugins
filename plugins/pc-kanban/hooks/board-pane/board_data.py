#!/usr/bin/env python3
"""Data source of the `/board` pane: kanban cards, session repos and full-text search, as JSON.

    board_data.py cards
    board_data.py session-repos <cwd>
    board_data.py search [--scope kanban,archive,logs] [--repos <slug>[,<slug>…]] -- <query>

The pane never parses a card itself: every fact it shows comes from here. Boards are discovered
from the process's working directory the way `stop_handoff.py --list-kanbans` does, and a card is
read with `stop_handoff.parse_frontmatter`. Prints one JSON document on stdout; exit 0 on success.
Python 3.11+, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))
import stop_handoff as sh  # noqa: E402

COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress", "4-done")
SCOPES = ("kanban", "archive", "logs")
MAX_HITS = 300
REPO_SCAN_DEPTH = 4

_CHANTIER_ID_RE = re.compile(r"CHANTIER(\d+)-")
_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_REMOTE_PREFIX_RE = re.compile(r"^(git@|https://|http://)[^:/]+[:/]")
_HANDOFF_RE = re.compile(
    r"\n*" + re.escape(sh.BLOCK_START) + r".*?" + re.escape(sh.BLOCK_END) + r"\n*",
    re.DOTALL,
)


class CardText(NamedTuple):
    """A card file split by `split_card`: its parsed frontmatter and its body."""

    frontmatter: dict[str, object]
    body: str


def split_card(text: str) -> CardText:
    """Split a card into its frontmatter and its body.

    Args:
        text: The whole card file.

    Returns:
        The parsed frontmatter (`{}` when the card has none) and the text after it, with the
        auto-handoff block removed and surrounding blank lines trimmed.
    """
    fm = sh.parse_frontmatter(text) or {}
    head = sh._FRONTMATTER_RE.match(text)
    body = text[head.end() :] if head else text
    return CardText(fm, _HANDOFF_RE.sub("\n", body).strip("\n"))


def card_title(body: str, slug: str) -> str:
    """Return a card's title: the first `# ` heading of its body, else its slug.

    Args:
        body: The card body, as `split_card` returns it.
        slug: The card's filename without `.md`.

    Returns:
        The heading text, or `slug` when the body has none.
    """
    found = _H1_RE.search(body)
    return found.group(1).strip() if found else slug


def card_age(fm: dict[str, object], today: date) -> int | None:
    """Return the days since a card's `date:` frontmatter.

    Args:
        fm: The card's parsed frontmatter.
        today: The reference day.

    Returns:
        The age in days, or None when `date:` is absent or not an ISO date.
    """
    raw = fm.get("date")
    if not isinstance(raw, str):
        return None
    try:
        return (today - date.fromisoformat(raw)).days
    except ValueError:
        return None


def board_cards(board: Path, today: date) -> list[dict[str, object]]:
    """Read every card of one board, `4-done` included.

    Args:
        board: A `_KANBAN` root.
        today: The reference day for `age_days`.

    Returns:
        One dict per card, ordered by chantier, column then slug.
    """
    cards: list[dict[str, object]] = []
    for column in COLUMNS:
        for path in board.glob(f"CHANTIER*/{column}/*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm, body = split_card(text)
            chantier = path.parent.parent
            ident = _CHANTIER_ID_RE.match(chantier.name)
            story = chantier / "story.md"
            cards.append(
                {
                    "slug": path.stem,
                    "repos": sh.card_repos(text),
                    "board": str(board),
                    "chantier": chantier.name,
                    "chantier_id": ident.group(1) if ident else None,
                    "column": column,
                    "path": str(path),
                    "title": card_title(body, path.stem),
                    "frontmatter": fm,
                    "priority": sh.card_priority(fm),
                    "body": body,
                    "story": str(story) if story.is_file() else None,
                    "age_days": card_age(fm, today),
                }
            )
    cards.sort(
        key=lambda c: (
            str(c["chantier"]),
            COLUMNS.index(str(c["column"])),
            str(c["slug"]),
        )
    )
    return cards


def all_cards(start: Path, today: date | None = None) -> list[dict[str, object]]:
    """Read the cards of every board visible from `start`.

    Args:
        start: The directory board discovery starts from.
        today: The reference day; defaults to the current date.

    Returns:
        The cards of every board, board after board.
    """
    today = today or date.today()
    return [c for board in sh.find_kanbans(start) for c in board_cards(board, today)]


def normalize(url: str) -> str:
    """Turn a git remote URL into `org/repo`, as `resolve_repos.sh` `normalize` does.

    Args:
        url: A remote URL, SSH or HTTP(S).

    Returns:
        The URL without its scheme, host and `.git` suffix; unchanged when it has no known prefix.
    """
    return re.sub(r"\.git$", "", _REMOTE_PREFIX_RE.sub("", url))


def _origin_slug(repo: Path) -> str | None:
    """Return `org/repo` of a repository's `origin` remote, None when it has none."""
    url = sh._git(repo, "remote", "get-url", "origin")
    return normalize(url) if url else None


def _git_dirs(root: Path) -> list[Path]:
    """List the repositories under `root`: directories holding a `.git`, `find -maxdepth 4` deep.

    Args:
        root: The directory to scan.

    Returns:
        The repository roots, sorted; `node_modules` is never entered.
    """
    found: list[Path] = []
    for here, dirs, files in os.walk(root):
        depth = len(Path(here).relative_to(root).parts)
        if ".git" in dirs or ".git" in files:
            found.append(Path(here))
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
        if depth + 1 >= REPO_SCAN_DEPTH:
            dirs[:] = []
    return sorted(found)


def session_repos(cwd: Path) -> dict[str, object]:
    """Resolve the repositories a session works in.

    Args:
        cwd: The session's directory.

    Returns:
        `{"in_repo": bool, "repos": [...]}`: the `origin` slug of the repository containing `cwd`,
        or, outside any repository, the slugs of every repository found under it.
    """
    if sh._git(cwd, "rev-parse", "--is-inside-work-tree") == "true":
        slug = _origin_slug(cwd)
        return {"in_repo": True, "repos": [slug] if slug else []}
    slugs = {s for repo in _git_dirs(cwd) if (s := _origin_slug(repo))}
    return {"in_repo": False, "repos": sorted(slugs)}


def _chantier_id(name: str) -> str | None:
    """Return the number of a `CHANTIER<NNNN>-…` directory name, None for any other name."""
    found = _CHANTIER_ID_RE.match(name)
    return found.group(1) if found else None


def _session_chantiers(board: Path, repos: list[str]) -> set[str]:
    """List the chantiers of a board with at least one card, live or archived, in `repos`.

    Args:
        board: A `_KANBAN` root.
        repos: The session's `org/repo` slugs.

    Returns:
        The chantier numbers; a card's repos are read with `sh.card_repos`.
    """
    live = [(p, 0) for c in COLUMNS for p in board.glob(f"CHANTIER*/{c}/*.md")]
    archived = [(p, 1) for p in board.glob("_ARCHIVE/CHANTIER*/**/*.md")]
    found: set[str] = set()
    # A YAML card declares `org/repo` slugs, a legacy `**Repos:**` card bare directory names.
    wanted = {
        identity for slug in repos for identity in (slug, slug.rsplit("/", 1)[-1])
    }
    for path, depth in [*live, *archived]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ident = _chantier_id(path.relative_to(board).parts[depth])
        if ident and set(sh.card_repos(text) or []) & wanted:
            found.add(ident)
    return found


def _scope_files(
    board: Path, scope: str, chantiers: set[str] | None = None
) -> list[Path]:
    """List the files one search scope covers on a board.

    Args:
        board: A `_KANBAN` root.
        scope: One of `SCOPES`.
        chantiers: When given, the only chantiers `kanban` and `archive` cover; `INDEX.md` is then left out.

    Returns:
        `kanban`: cards of every column, stories and `INDEX.md`; `archive`: everything under `_ARCHIVE`;
        `logs`: everything under the board's agent-log directory, never filtered.
    """

    def keep(path: Path, depth: int) -> bool:
        return (
            chantiers is None
            or _chantier_id(path.relative_to(board).parts[depth]) in chantiers
        )

    if scope == "kanban":
        live = [p for c in COLUMNS for p in board.glob(f"CHANTIER*/{c}/*.md")]
        files = [*live, *board.glob("CHANTIER*/story.md")]
        return sorted(
            [p for p in files if keep(p, 0)]
            + ([board / "INDEX.md"] if chantiers is None else [])
        )
    root = board / "_ARCHIVE" if scope == "archive" else sh.agent_log_dir(board)
    files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
    return [p for p in files if keep(p, 1)] if scope == "archive" else files


def _rel(path: Path, board: Path) -> str:
    """Return `path` under `board`, or the whole path when it lies elsewhere."""
    try:
        return str(path.relative_to(board))
    except ValueError:
        return str(path)


def search(
    start: Path,
    query: str,
    scopes: list[str] | None = None,
    repos: list[str] | None = None,
) -> dict[str, object]:
    """Search every board for a literal, case-insensitive query in whole files.

    Args:
        start: The directory board discovery starts from.
        query: The text to find.
        scopes: The scopes whose hits are listed; counts cover every scope regardless.
        repos: The session's repos; when non-empty, `kanban` and `archive` cover only the chantiers
            with a card in them (see `_session_chantiers`). Empty or None: every chantier.

    Returns:
        `{"query", "counts", "hits", "truncated"}`: `counts` is the number of matching lines per
        scope; `hits` lists `{scope, path, rel, line, text}` for the selected scopes, at most
        `MAX_HITS`, `truncated` saying whether more existed.
    """
    scopes = scopes if scopes is not None else list(SCOPES)
    needle = query.lower()
    counts = dict.fromkeys(SCOPES, 0)
    hits: list[dict[str, object]] = []
    truncated = False
    if needle:
        for board in sh.find_kanbans(start):
            chantiers = _session_chantiers(board, repos) if repos else None
            for scope in SCOPES:
                for path in _scope_files(board, scope, chantiers):
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for number, line in enumerate(text.splitlines(), start=1):
                        if needle not in line.lower():
                            continue
                        counts[scope] += 1
                        if scope not in scopes:
                            continue
                        if len(hits) >= MAX_HITS:
                            truncated = True
                            continue
                        hits.append(
                            {
                                "scope": scope,
                                "path": str(path),
                                "rel": _rel(path, board),
                                "line": number,
                                "text": line.strip(),
                            }
                        )
    return {"query": query, "counts": counts, "hits": hits, "truncated": truncated}


def parse_scopes(raw: str) -> list[str]:
    """Parse a `--scope` value.

    Args:
        raw: Comma-separated scope names.

    Returns:
        The scopes in `SCOPES` order.

    Raises:
        ArgumentTypeError: When a name is not one of `SCOPES`.
    """
    names = {s.strip() for s in raw.split(",") if s.strip()}
    unknown = names - set(SCOPES)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown scope: {', '.join(sorted(unknown))}")
    return [s for s in SCOPES if s in names]


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and print its JSON result.

    Args:
        argv: The arguments after the program name; defaults to `sys.argv[1:]`.

    Returns:
        0.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cards")
    sub.add_parser("session-repos").add_argument("cwd")
    find = sub.add_parser("search")
    find.add_argument("query")
    find.add_argument("--scope", type=parse_scopes, default=list(SCOPES))
    find.add_argument(
        "--repos", type=lambda raw: [r for r in raw.split(",") if r], default=[]
    )
    args = parser.parse_args(argv)
    here = Path(os.getcwd())
    if args.command == "cards":
        result: object = all_cards(here)
    elif args.command == "session-repos":
        result = session_repos(Path(args.cwd))
    else:
        result = search(here, args.query, args.scope, args.repos)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
