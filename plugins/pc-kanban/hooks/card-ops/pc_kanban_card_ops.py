#!/usr/bin/env python3
"""Move one kanban card by slug: archive it, drop it, or select it for development.

    pc_kanban_card_ops.py [--board DIR]... archive <slug>
    pc_kanban_card_ops.py [--board DIR]... drop <slug> <reason...>
    pc_kanban_card_ops.py [--board DIR]... select <slug> [--force]

The slug is the card's filename without `.md`. It is resolved across the kanban boards in the
columns the verb accepts; zero or several matches is an error, never a guess. `archive` and `drop`
delegate to the `chantier` skill's `archive_card.sh` / `drop_card.sh`; `select` moves the card from
`1-todo/` to `2-selected-for-dev/` once it meets the selection conditions (`selection_problems`).

Without `--board`, the boards are those `stop_handoff.py --list-kanbans` reports. Python 3.12+,
stdlib only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CHANTIER_SCRIPTS = PLUGIN_ROOT / "skills" / "chantier" / "scripts"
COLUMNS = {
    "archive": ("4-done", "3-in-progress"),
    "drop": ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress"),
    "select": ("1-todo",),
}
MODELS = ("haiku", "sonnet", "opus", "fable")
EMPTY = ("", "null", "~")
ID_RE = re.compile(r"\b[A-Z]+-\d{4}-\d+\b")


def default_boards() -> list[Path]:
    """List the kanban boards the session can see.

    Returns:
        The board roots reported by `stop_handoff.py --list-kanbans`; empty when it fails.
    """
    done = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "hooks" / "stop_handoff.py"),
            "--list-kanbans",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return [Path(line.strip()) for line in done.stdout.splitlines() if line.strip()]


def _resolve(slug: str, columns: tuple[str, ...], boards: list[Path]) -> Path:
    """Find the one card named `slug` in `columns` of any board.

    Args:
        slug: The card filename without `.md`.
        columns: The column directories to look into.
        boards: The board roots.

    Returns:
        The card path.

    Raises:
        SystemExit: When no card or several cards match.
    """
    found = [
        card
        for board in boards
        for column in columns
        for card in board.glob(f"CHANTIER*/{column}/{slug}.md")
    ]
    if not found:
        raise SystemExit(f"no card '{slug}' in {', '.join(columns)}")
    if len(found) > 1:
        raise SystemExit(
            "several cards match, pass a path to the script instead:\n"
            + "\n".join(map(str, found))
        )
    return found[0]


def frontmatter(text: str) -> list[str]:
    """Return the lines of a card's YAML frontmatter.

    Args:
        text: The whole card.

    Returns:
        The lines between the opening and closing `---`; empty when there is none.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    return lines[1:end] if end else []


def field(head: list[str], key: str) -> str:
    """Read one scalar frontmatter field.

    Args:
        head: The frontmatter lines.
        key: The field name.

    Returns:
        The value without comment or quotes; empty when the key is absent.
    """
    for line in head:
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].split("#", 1)[0].strip().strip("'\"")
    return ""


def blocker_state(slug: str, boards: list[Path]) -> str:
    """Tell where a `blocked_by` card stands.

    Args:
        slug: The blocking card's filename without `.md`.
        boards: The board roots.

    Returns:
        `done` (in a `4-done/` or a `-done` archive), `dropped`, or `open`.
    """
    for board in boards:
        if any(board.glob(f"CHANTIER*/4-done/{slug}.md")) or any(
            board.glob(f"_ARCHIVE/CHANTIER*-done/*_{slug}.md")
        ):
            return "done"
        if any(board.glob(f"_ARCHIVE/CHANTIER*-dropped/*_{slug}.md")):
            return "dropped"
    return "open"


def requirement_ids(head: list[str]) -> list[str]:
    """Read the ids under a card's `requirements` key (inline or multi-line list).

    Args:
        head: The frontmatter lines.

    Returns:
        The requirement ids, in order.
    """
    ids: list[str] = []
    inside = False
    for line in head:
        if line.startswith("requirements:"):
            inside = True
        elif re.match(r"[A-Za-z_][\w-]*:", line):
            inside = False
        if inside:
            ids += ID_RE.findall(line)
    return ids


def selection_problems(
    card: Path, text: str, boards: list[Path], force: bool = False
) -> list[str]:
    """List what keeps a card from being selected for development.

    Args:
        card: The card in `1-todo/`.
        text: The whole card.
        boards: The board roots.
        force: Skip the requirements check only; every other condition still applies.

    Returns:
        One message per unmet condition; empty when the card is selectable.
    """
    head = frontmatter(text)
    problems = []
    ids = [] if force else requirement_ids(head)
    story = card.parent.parent / "story.md"
    declared = story.read_text(encoding="utf-8") if story.is_file() else ""
    if not ids and not force:
        problems.append(
            "no `requirements:` — a card traces to at least one requirement"
        )
    problems += [
        f"requirement {i} is not declared in the chantier's story"
        for i in ids
        if not re.search(rf"(?m)^\|\s*{re.escape(i)}\s*\|", declared)
    ]
    if field(head, "model") not in MODELS:
        problems.append(
            f"no valid `model:` ({', '.join(MODELS)}) — mandatory from selected on"
        )
    if not re.search(r"(?m)^## Done when", text):
        problems.append("no `## Done when` section — set at selection")
    blocker = field(head, "blocked_by")
    if blocker not in EMPTY:
        state = blocker_state(blocker, boards)
        if state == "dropped":
            problems.append(
                f"blocked_by '{blocker}' was dropped — run the obsolescence check first"
            )
        elif state == "open":
            problems.append(f"blocked_by '{blocker}' is not done")
    return problems


def select(card: Path, boards: list[Path], force: bool = False) -> int:
    """Move a card from `1-todo/` to `2-selected-for-dev/` and stamp the selection date.

    Args:
        card: The card in `1-todo/`.
        boards: The board roots.
        force: Select even if the card traces to no requirement.

    Returns:
        The exit status; 1 when a selection condition is unmet or the target exists.
    """
    text = card.read_text(encoding="utf-8")
    problems = selection_problems(card, text, boards, force)
    dest = card.parent.parent / "2-selected-for-dev" / card.name
    if dest.exists():
        problems.append(f"{dest} already exists")
    if problems:
        print(f"{card.name} not selected:", *problems, sep="\n  - ", file=sys.stderr)
        return 1
    today = f"date: {date.today().isoformat()}"
    stamped = (
        re.sub(r"(?m)^date:.*$", today, text, count=1)
        if field(frontmatter(text), "date")
        else text.replace("---\n", f"---\n{today}\n", 1)
    )
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(stamped, encoding="utf-8")
    card.unlink()
    print(f"selected -> {dest}")
    return 0


def run_script(script: str, *args: str) -> int:
    """Run one of the `chantier` skill's bash scripts and relay its output.

    Args:
        script: The script filename.
        *args: Its arguments.

    Returns:
        The script's exit status.
    """
    done = subprocess.run(
        ["bash", str(CHANTIER_SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    return done.returncode


def main(argv: list[str] | None = None) -> int:
    """Resolve the card and run the verb.

    Args:
        argv: The arguments; defaults to `sys.argv[1:]`.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--board", action="append", type=Path, default=[])
    parser.add_argument("verb", choices=sorted(COLUMNS))
    parser.add_argument("slug")
    parser.add_argument("reason", nargs="*")
    parser.add_argument(
        "--force", action="store_true", help="select without requirements"
    )
    args = parser.parse_args(argv)
    if args.verb == "drop" and not args.reason:
        parser.error("drop needs a reason")
    boards = args.board or default_boards()
    card = _resolve(args.slug, COLUMNS[args.verb], boards)
    if args.verb == "select":
        return select(card, boards, args.force)
    if args.verb == "archive":
        return run_script("archive_card.sh", str(card))
    return run_script("drop_card.sh", str(card), " ".join(args.reason))


if __name__ == "__main__":
    sys.exit(main())
