#!/usr/bin/env python3
"""Move one kanban card by slug: archive it, drop it, or select it for development.

    pc_kanban_card_ops.py [--board DIR]... archive <slug>
    pc_kanban_card_ops.py [--board DIR]... drop <slug> <reason...>
    pc_kanban_card_ops.py [--board DIR]... select <slug> [--force]

The slug is the card's filename without `.md`. It is resolved across the kanban boards in the
columns the verb accepts; zero or several matches is an error, never a guess. `archive` and `drop`
move the card to `_ARCHIVE/` and print the closing sequence (open cards, next-card candidates,
session recommendation); `select` moves the card from
`1-todo/` to `2-selected-for-dev/` once it meets the selection conditions (`selection_problems`).

Without `--board`, the boards are those `stop_handoff.py --list-kanbans` reports. Python 3.12+,
stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "kanban" / "scripts"))
import next_card_recommendation as ncr  # noqa: E402
import open_cards_brief as ob  # noqa: E402

SESSION_THRESHOLD_PCT = 40
COLUMNS = {
    "archive": ("4-done", "3-in-progress"),
    "drop": ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress"),
    "select": ("1-todo",),
}
OPEN_COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress")
MODELS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MOCKUP_AXES = ("layout", "design", "content", "behavior")
MOCKUP_LEVELS = ("strict", "guided", "free")
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


def requirements_section(text: str) -> list[str]:
    """Collect the lines under every `## Requirements` heading of the given card texts.

    Args:
        text: One or several cards, concatenated.

    Returns:
        The lines of each `## Requirements` section.
    """
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == "Requirements"
        elif inside:
            lines.append(line)
    return lines


def mockup_problems(head: list[str]) -> list[str]:
    """List what is wrong with a card's `mockup:` and `mockup_fidelity:`.

    Args:
        head: The frontmatter lines.

    Returns:
        One message per refusal; empty when both fields are absent or valid.
    """
    mockup = field(head, "mockup")
    fidelity = field(head, "mockup_fidelity")
    problems = []
    if mockup in EMPTY:
        if fidelity not in EMPTY:
            problems.append(
                "`mockup_fidelity:` without a `mockup:` — declare the mockup"
            )
        return problems
    path = Path(mockup).expanduser()
    if not path.is_absolute():
        problems.append(f"`mockup: {mockup}` is not an absolute (or `~/`) path")
    elif not path.exists():
        problems.append(f"`mockup: {mockup}` does not exist")
    block_style = any(
        line.startswith("mockup_fidelity:") and nxt.lstrip().startswith("- ")
        for line, nxt in zip(head, head[1:])
    )
    if block_style or (
        fidelity not in EMPTY
        and not (fidelity.startswith("[") and fidelity.endswith("]"))
    ):
        problems.append(
            f"`mockup_fidelity: {fidelity}` is not a bracketed list — "
            "write `[layout=strict, design=free]`"
        )
        return problems
    inner = fidelity[1:-1] if fidelity not in EMPTY else ""
    seen: set[str] = set()
    for item in (i.strip() for i in inner.split(",")) if inner.strip() else ():
        axis, sep, level = item.partition("=")
        if axis.strip() in seen:
            problems.append(f"`mockup_fidelity` names the axis '{axis.strip()}' twice")
        seen.add(axis.strip())
        if not sep:
            problems.append(f"`mockup_fidelity` element '{item}' is not `axe=niveau`")
        elif axis.strip() not in MOCKUP_AXES:
            problems.append(
                f"`mockup_fidelity` axis '{axis.strip()}' is not one of "
                f"{', '.join(MOCKUP_AXES)}"
            )
        elif level.strip() not in MOCKUP_LEVELS:
            problems.append(
                f"`mockup_fidelity` level '{level.strip()}' is not one of "
                f"{', '.join(MOCKUP_LEVELS)}"
            )
    return problems


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
    declared = "\n".join(
        c.read_text(encoding="utf-8", errors="ignore")
        for col in OPEN_COLUMNS
        for c in sorted((card.parent.parent / col).glob("*.md"))
    )
    story = card.parent.parent / "story.md"
    if story.is_file():  # une story pas encore migrée garde sa table de requirements
        declared += "\n" + story.read_text(encoding="utf-8", errors="ignore")
    if not ids and not force:
        problems.append(
            "no `requirements:` — a card traces to at least one requirement"
        )
    problems += [
        f"requirement {i} is not defined in any open card's `## Requirements` (nor the story's)"
        for i in ids
        if not any(
            re.search(rf"^\|\s*{re.escape(i)}\s*\|", line)
            for line in requirements_section(declared)
        )
    ]
    if field(head, "model") not in MODELS:
        problems.append(
            f"no valid `model:` ({', '.join(MODELS)}) — mandatory from selected on"
        )
    effort = field(head, "effort")
    if effort not in EMPTY and effort not in EFFORTS:
        problems.append(
            f"`effort: {effort}` is not one of {', '.join(EFFORTS)} — or omit it"
        )
    problems += mockup_problems(head)
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


def _seed_nav(dest_dir: Path) -> None:
    """Create an archive folder with its `.nav.yml` (newest first) when missing.

    Args:
        dest_dir: The archive folder.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    nav = dest_dir / ".nav.yml"
    if not nav.is_file():
        nav.write_text("sort:\n  direction: desc\n  by: filename\n", encoding="utf-8")
        print(f"seeded {nav.parent}/.nav.yml")


def _archive_destination(card: Path, kind: str) -> Path | None:
    """Pick the dated archive path of a card, seeding its folder; refuse an existing entry.

    Args:
        card: The card to move.
        kind: `done` or `dropped`.

    Returns:
        The destination path; None (error printed) when it already exists.
    """
    chantier = card.parent.parent
    dest_dir = chantier.parent / "_ARCHIVE" / f"{chantier.name.split('-', 1)[0]}-{kind}"
    _seed_nav(dest_dir)
    dest = dest_dir / f"{date.today().isoformat()}_{card.name}"
    if dest.exists():
        print(f"refusing to overwrite existing archive entry: {dest}", file=sys.stderr)
        return None
    return dest


def archive(card: Path, boards: list[Path]) -> int:
    """Archive a done card, or a reviewed one straight from `3-in-progress/`.

    Args:
        card: The card in `4-done/` or `3-in-progress/`.
        boards: The board roots.

    Returns:
        The exit status; 1 when the reviewed box is unchecked or the target exists.
    """
    if card.parent.name == "3-in-progress" and "- [x] reviewed (" not in card.read_text(
        encoding="utf-8"
    ):
        print(
            f"card in 3-in-progress/ has no checked reviewed box: {card}\n"
            "close it via 4-done, or check the reviewed box first (finishing-a-card.md)",
            file=sys.stderr,
        )
        return 1
    dest = _archive_destination(card, "done")
    if dest is None:
        return 1
    chantier = card.parent.parent
    card.rename(dest)
    print(f"archived -> {dest}")
    print(closing_sequence(chantier, dest, boards, current_signals()))
    return 0


def drop(card: Path, reason: str, boards: list[Path]) -> int:
    """Drop a card confirmed obsolete, appending the reason to it.

    Args:
        card: The card in an open column.
        reason: Why it is dropped.
        boards: The board roots.

    Returns:
        The exit status; 1 when the reason is empty or the target exists.
    """
    if not reason.strip():
        print("a reason is required", file=sys.stderr)
        return 1
    dest = _archive_destination(card, "dropped")
    if dest is None:
        return 1
    chantier = card.parent.parent
    card.rename(
        dest
    )  # move first: a failed append leaves an archive without reason, never a mutated open card
    with dest.open("a", encoding="utf-8") as out:
        out.write(f"\n## Dropped ({date.today().isoformat()})\n\n{reason}\n")
    print(f"dropped -> {dest}")
    print(closing_sequence(chantier, dest, boards, current_signals()))
    return 0


def priority(head: list[str]) -> int:
    """Read a card's `priority:` (1-5).

    Args:
        head: The frontmatter lines.

    Returns:
        The priority; 3 when omitted or not a number.
    """
    value = field(head, "priority")
    return int(value) if value.isdigit() else 3


def candidates(chantier: Path, boards: list[Path]) -> list[tuple[Path, str]]:
    """List the `1-todo/` cards worth proposing next, each with its note.

    Args:
        chantier: The chantier directory.
        boards: The board roots.

    Returns:
        `(card, note)` pairs without the cards whose blocker is still open, by `priority`
        descending then `date:` ascending; a card without `date:` is malformed, noted and last,
        and a card whose blocker was dropped is noted (`select` refuses it until the obsolescence
        check).
    """
    keyed = []
    for card in sorted((chantier / "1-todo").glob("*.md")):
        head = frontmatter(card.read_text(encoding="utf-8", errors="ignore"))
        blocker = field(head, "blocked_by")
        state = blocker_state(blocker, boards) if blocker not in EMPTY else "done"
        if state == "open":
            continue
        notes = []
        if state == "dropped":
            notes.append("blocker dropped — obsolescence check first")
        day = field(head, "date")
        if not day:
            notes.append("no date: — malformed")
        keyed.append(((not day, -priority(head), day), card, "; ".join(notes)))
    return [(card, note) for _, card, note in sorted(keyed, key=lambda k: k[0])]


def current_signals() -> dict:
    """Read the session's context-window and rate-limit signals.

    Returns:
        A dict with `context_window_pct` (int or None) and `rate_limit` (dict or None).
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return {
        "context_window_pct": ncr.context_window_pct(session_id, os.getcwd()),
        "rate_limit": ncr.evaluate_rate_limit(session_id),
    }


def recommendation(signals: dict, threshold: int = SESSION_THRESHOLD_PCT) -> str:
    """Turn the session signals into the session-vs-spawn line.

    Args:
        signals: What `current_signals` returns.
        threshold: The context percentage from which a new session is advised.

    Returns:
        A pause line when a rate-limit window is triggered, else a continue or new-session line
        from the context percentage; "" when the context is unknown.
    """
    for window, info in (signals.get("rate_limit") or {}).items():
        if info.get("triggered"):
            return (
                f"Reco: wait for the {window} reset (resets_at {info['resets_at']}, "
                f"{info['used_percentage']}% used) before starting another card."
            )
    pct = signals.get("context_window_pct")
    if pct is None:
        return ""
    if pct >= threshold:
        return f"Reco: start a new session for the next card (context at {pct}%)."
    return f"Reco: continue in this session (context at {pct}%)."


def closing_sequence(
    chantier: Path,
    closed: Path,
    boards: list[Path],
    signals: dict,
    threshold: int = SESSION_THRESHOLD_PCT,
) -> str:
    """Build the closing sequence printed after an archive or a drop.

    Args:
        chantier: The closed card's chantier directory.
        closed: The closed card, now in `_ARCHIVE/`.
        boards: The board roots.
        signals: What `current_signals` returns.
        threshold: The context percentage from which a new session is advised.

    Returns:
        The obsolescence step, the next-card candidates, the recommendation, and the `/rename`
        step only when the context is at `threshold` or more.
    """
    briefs = ob.render_open_cards(ob.open_cards(chantier))
    lines = [
        "",
        "Closing sequence (finishing-a-card.md):",
        f"1. Obsolescence — open cards of {chantier.name}:",
        briefs or "  (none open)",
        f"   Then write `- [x] obsolescence (none | <slug>, ...)` on {closed}.",
        "2. Next card — candidates from 1-todo/ (priority desc, then date asc):",
    ]
    found = candidates(chantier, boards)
    lines += [
        f"   - {c.stem}" + (f" ({note})" if note else "") for c, note in found
    ] or ["   nothing to propose"]
    reco = recommendation(signals, threshold)
    pct = signals.get("context_window_pct")
    if reco:
        lines.append(f"3. {reco}")
    if pct is not None and pct >= threshold:
        lines.append(
            f"4. End of session: propose `/rename <slug>` (context at {pct}%, "
            "see finishing-a-card.md § Naming the session)."
        )
    return "\n".join(lines)


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
        return archive(card, boards)
    return drop(card, " ".join(args.reason), boards)


if __name__ == "__main__":
    sys.exit(main())
