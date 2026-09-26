#!/usr/bin/env python3
"""Build the "export" leg's zip — select cards by id, move them to `3-in-progress` with an
export marker, and assemble the archive an external agent receives.

The repo code subtree is computed elsewhere (`export_subtree`, the `pc-delegate` MCP tool — the
one place a third-party dependency, `grimp`, is allowed in this plugin) and passed in here as a
plain list of repo-relative paths; this script's own job is deterministic: card selection and
state, the secrets denylist, and zipping — no reasoning, no network, no repo-graph computation.

Sequencing is deliberate: every card's new content is computed first (pure — nothing written), the
zip is built and fully written next, and only once that succeeds are the cards actually moved on
disk. A failure at any point before the last step leaves the board untouched — never a card marked
`exported:` for an archive that was never produced, and never a partially written zip left behind
(`--out` is removed on failure).

Usage:
    python3 build_export.py --chantier PATH --card ID [--card ID ...] --repo PATH
        [--repo-file REL_PATH ...] [--agent-log-dir PATH] [--guide PATH] --out OUT.zip [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import date
from pathlib import Path

import secrets_filter

_COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress", "4-done")


class ExportError(Exception):
    pass


def _reject_path_escape(component: str, *, what: str) -> None:
    """A card id or a `--repo-file` entry is a single path component / relative path meant to
    stay inside its own root — never an absolute path, never a `..` segment. Raises `ExportError`
    otherwise; called before that string is ever joined onto a real directory."""
    if component.startswith(("/", "\\")) or ".." in Path(component).parts:
        raise ExportError(f"{what} escapes its root: {component}")


def find_card(chantier_dir: Path, card_id: str) -> Path:
    """Locate `<card_id>.md` across the chantier's columns — exactly one match, or an error."""
    _reject_path_escape(card_id, what="card id")
    matches = [
        chantier_dir / col / f"{card_id}.md"
        for col in _COLUMNS
        if (chantier_dir / col / f"{card_id}.md").is_file()
    ]
    if not matches:
        raise ExportError(f"card not found in any column: {card_id}")
    if len(matches) > 1:
        raise ExportError(f"card found in more than one column: {card_id}")
    return matches[0]


def set_frontmatter_field(content: str, field: str, value: str) -> str:
    """Set `field: value` in the YAML frontmatter block, replacing an existing `field:` line or
    appending one just before the closing `---`. The card's body (everything after the second
    `---`) is untouched."""
    if "\n" in value:
        raise ExportError(f"frontmatter value for {field!r} cannot contain a newline")
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ExportError("card has no frontmatter block")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ExportError("card frontmatter block is not closed")
    body = [line for line in lines[1:end] if not line.startswith(f"{field}:")]
    body.append(f"{field}: {value}\n")
    return "".join([lines[0], *body, lines[end]] + lines[end + 1 :])


def _exported_content(card_path: Path, *, today: str) -> str:
    """*card_path*'s content with `date:` bumped to *today* and an `exported:` marker set — the
    machine-readable signal distinguishing this from a card taken into progress locally. Pure —
    reads the file, writes nothing."""
    content = card_path.read_text(encoding="utf-8")
    content = set_frontmatter_field(content, "date", today)
    content = set_frontmatter_field(content, "exported", today)
    return content


def _apply_export(card_path: Path, chantier_dir: Path, content: str) -> None:
    """Write *content* to `3-in-progress/<card_path.name>` (a no-op move if already there) and
    remove *card_path* if it moved. The zip this content went into must already be written
    successfully before this is ever called — see the module docstring."""
    dest = chantier_dir / "3-in-progress" / card_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    if card_path.resolve() != dest.resolve():
        card_path.unlink()


def chantier_tree_listing(chantier_dir: Path) -> str:
    """A flat, sorted listing of every card path in the chantier — orientation for the external
    agent, not the full content of every card (kept out deliberately: unrelated card bodies are
    not this export's business, and may carry information the selected cards do not need to
    leave with)."""
    paths = sorted(
        p.relative_to(chantier_dir).as_posix()
        for col in _COLUMNS
        for p in (chantier_dir / col).glob("*.md")
        if (chantier_dir / col).is_dir()
    )
    return "\n".join(paths) + "\n"


def filtered_agent_log(agent_log_dir: Path) -> dict[str, str]:
    """Every `.md` file under *agent_log_dir*, denylist-excluded and local-path-redacted —
    `{repo-relative-name: content}`."""
    result: dict[str, str] = {}
    for p in sorted(agent_log_dir.glob("*.md")):
        rel = f"_AGENT_LOG/{p.name}"
        if secrets_filter.is_denied(rel):
            continue
        result[rel] = secrets_filter.redact_local_paths(p.read_text(encoding="utf-8"))
    return result


def _resolve_repo_files(
    repo: Path, repo_files: list[str]
) -> tuple[list[Path], list[str]]:
    """*repo_files* (repo-relative) resolved and validated against *repo*'s root — containment
    checked the same way the "reintegrate" leg's own `apply_reintegrate.py` checks it — split into
    (kept, denylist-excluded). Raises `ExportError` on a `--repo-file` that escapes *repo*, never
    silently drops it: an escape attempt is a bug in the caller, not a routine exclusion."""
    repo_resolved = repo.resolve()
    kept: list[Path] = []
    excluded: list[str] = []
    for rel in repo_files:
        _reject_path_escape(rel, what="repo file")
        candidate = (repo_resolved / rel).resolve()
        if not candidate.is_relative_to(repo_resolved):
            raise ExportError(f"repo file escapes repo root: {rel}")
        safe_rel = candidate.relative_to(repo_resolved).as_posix()
        if secrets_filter.is_denied(safe_rel):
            excluded.append(safe_rel)
            continue
        if not candidate.is_file():
            raise ExportError(f"not a file: {rel}")
        kept.append(candidate)
    return kept, excluded


def suggested_prompt(card_ids: list[str]) -> str:
    """A ready-to-paste message for the external agent's chat — the deliverable-format contract
    (zip layout, `REPORT.md`) is identical on every export, so it is generated here once instead
    of retyped by hand each time; the task itself is not restated — `AGENTS.md` already tells the
    agent to read `cards/<id>.md` for it. English, unconditionally — a public plugin never
    hardcodes a chat language; a caller who wants another one translates this before pasting it."""
    cards_line = ", ".join(card_ids)
    return (
        f"I've uploaded a zip containing AGENTS.md (read it first — it's the instructions) and "
        f"cards/<id>.md for each card ({cards_line}) — the exact task is described there.\n"
        "\n"
        "If a hook or test covering a file you changed is included under repo/ and your "
        "environment can execute code, run it and paste the command, its output, and its "
        "exit code into REPORT.md before you finish.\n"
        "\n"
        "When you're done, give me back a zip containing:\n"
        "- only the files you created or modified, paths preserved exactly as in the archive "
        "you received (not under repo/)\n"
        "- a REPORT.md at the root of the zip: what you did, any scope decisions you made, any "
        "bug/issue you found but left alone because it was out of scope, and anything a human "
        "reviewer should know before trusting the change.\n"
    )


def build_zip(
    out: Path,
    *,
    cards: dict[str, str],
    chantier_tree: str,
    agent_log: dict[str, str],
    guide_content: str | None,
    repo: Path | None,
    repo_paths: list[Path],
) -> None:
    """Write *out* — every path in *repo_paths* is already validated and denylist-filtered by
    `_resolve_repo_files`; this function does no filtering of its own."""
    repo_resolved = repo.resolve() if repo is not None else None
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in cards.items():
            zf.writestr(f"cards/{name}", secrets_filter.redact_local_paths(content))
        zf.writestr("CHANTIER_TREE.txt", chantier_tree)
        for name, content in agent_log.items():
            zf.writestr(name, content)
        if guide_content is not None:
            zf.writestr("AGENTS.md", guide_content)
        if repo_resolved is not None:
            for candidate in repo_paths:
                safe_rel = candidate.relative_to(repo_resolved).as_posix()
                zf.write(candidate, arcname=f"repo/{safe_rel}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chantier", required=True, type=Path)
    parser.add_argument("--card", action="append", required=True, dest="card_ids")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--repo-file", action="append", default=[], dest="repo_files")
    parser.add_argument("--agent-log-dir", type=Path)
    parser.add_argument("--guide", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        today = date.today().isoformat()
        card_paths = [find_card(args.chantier, cid) for cid in args.card_ids]
        cards = {
            f"{card_id}.md": _exported_content(card_path, today=today)
            for card_id, card_path in zip(args.card_ids, card_paths, strict=True)
        }

        chantier_tree = chantier_tree_listing(args.chantier)
        agent_log = filtered_agent_log(args.agent_log_dir) if args.agent_log_dir else {}
        guide_content = args.guide.read_text(encoding="utf-8") if args.guide else None
        repo_paths, excluded = (
            _resolve_repo_files(args.repo, args.repo_files)
            if args.repo is not None
            else ([], [])
        )

        if args.dry_run:
            print(
                f"would export {len(cards)} card(s), {len(agent_log)} agent log file(s), "
                f"{len(repo_paths)} repo file(s)"
            )
            for rel in excluded:
                print(f"🚫 would exclude (secrets denylist): {rel}")
            return 0

        try:
            build_zip(
                args.out,
                cards=cards,
                chantier_tree=chantier_tree,
                agent_log=agent_log,
                guide_content=guide_content,
                repo=args.repo,
                repo_paths=repo_paths,
            )
        except OSError:
            args.out.unlink(missing_ok=True)
            raise

        for card_path, content in zip(card_paths, cards.values(), strict=True):
            _apply_export(card_path, args.chantier, content)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"wrote {args.out} — {len(cards)} card(s), {len(repo_paths)} repo file(s)")
    for rel in excluded:
        print(f"🚫 excluded (secrets denylist): {rel}")
    print("\n--- suggested prompt for the external agent's chat ---")
    print(suggested_prompt(args.card_ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
