#!/usr/bin/env python3
"""Reintegrate a zip (or directory) of files an external agent produced, into a local repo.

A file already tracked, and a brand-new one, are both accepted — the contract is "only what the
external agent created or modified, tree-relative paths preserved", not "only files already
tracked". What is still never accepted silently:

- **A `..`/absolute/drive-letter entry, or one that resolves (following any local symlink) outside
  the repo root** — refused outright, always. The containment check is applied to the fully
  resolved path, not to the raw entry string, so a symlinked directory already present in the repo
  can never redirect a write elsewhere.
- **A target whose local copy has diverged since the export** — a tracked file with an uncommitted
  diff against HEAD, or an untracked file already sitting at that path with different content.
  Refused and reported; never silently overwritten.
- **A write that cannot succeed** (a directory already sits at the exact target path, or a
  non-directory file — or an unresolvable symlink — blocks one of its parent segments) — detected
  before any write is attempted, so `--dry-run` predicts it the same way a real run would fail it,
  instead of the real run crashing partway through the batch. Which of the two pre-flight checks
  catches a given symlink cycle can vary by Python version; both fail closed either way.

Every applied `.py` file is classified `comments-only` vs `code-changed` by AST comparison
(docstrings masked). A brand-new file has no "old" version to compare against, so it always
classifies `code-changed`.

A top-level `EXPORTED_CARDS.txt` is reserved too: never applied, read by `read_manifest` to
determine the cards the return covers.

A top-level `REPORT.md` in the source (no directory prefix — `sub/REPORT.md` is an ordinary repo
file) is reserved: never applied into the repo, always pulled out and either written as a new
`_AGENT_LOG` entry (when `--agent-log-dir` is given) or printed in full so a human can act on it —
the narrative an external agent hands back (what it did, judgment calls, anything found but out of
scope) otherwise has nowhere durable to land.

Usage:
    python3 apply_reintegrate.py <source.zip-or-dir> [--repo PATH] [--agent-log-dir PATH]
        [--card ID ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_REPORT_ENTRY = "REPORT.md"
_MANIFEST_ENTRY = "EXPORTED_CARDS.txt"


def _is_report_entry(entry_name: str) -> bool:
    """True for a top-level `REPORT.md` only — `sub/REPORT.md` is an ordinary repo file, not the
    reserved report."""
    normalized = entry_name.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == _REPORT_ENTRY


def _is_manifest_entry(entry_name: str) -> bool:
    """True for a top-level `EXPORTED_CARDS.txt` only — the exported-slugs file is read by
    `read_manifest`, never applied into the repo."""
    normalized = entry_name.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == _MANIFEST_ENTRY


def _report_log_name(*, card_ids: list[str], repo: Path, today: str) -> str:
    slug = "-".join(card_ids) if card_ids else repo.name
    return f"{today}_reintegrate-{slug}.md"


def _mask_docstrings(tree: ast.AST) -> ast.AST:
    """Blank every docstring (a module/class/function's leading string-expression statement) in
    place, so an `ast.dump()` diff reflects only executable structure, never narrative text —
    comments are already absent from the AST on their own, nothing to mask for those."""
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                first.value.value = ""
    return tree


def classify_change(old: bytes, new: bytes) -> str:
    """'unchanged', 'comments-only', 'code-changed', or 'unparseable' — the last never gets
    silently treated as safe, a caller must review it by hand."""
    if old == new:
        return "unchanged"
    try:
        old_tree = _mask_docstrings(ast.parse(old))
        new_tree = _mask_docstrings(ast.parse(new))
    except SyntaxError:
        return "unparseable"
    return (
        "comments-only" if ast.dump(old_tree) == ast.dump(new_tree) else "code-changed"
    )


def _tracked_files(repo: Path) -> set[str]:
    """Every path `git` tracks, exactly as stored — raw bytes, NUL-separated, `quotePath`
    disabled — so a non-ASCII filename compares equal to the same bytes coming from a zip entry
    instead of git's own C-style-escaped default rendering."""
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotePath=false", "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    return set(result.stdout.decode("utf-8").split("\0")) - {""}


def _has_head(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def is_dirty(repo: Path, rel_path: str, *, head_exists: bool) -> bool:
    """False when there is no `HEAD` yet — nothing can have diverged from a commit that does
    not exist, so treating that as dirty would mislabel a same-content re-apply as a conflict."""
    if not head_exists:
        return False
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "HEAD", "--", rel_path],
        capture_output=True,
        check=False,
    )
    return result.returncode != 0


def resolve_within_repo(repo_resolved: Path, entry_name: str) -> Path | None:
    """The absolute path *entry_name* resolves to under *repo_resolved*, or None when it must be
    refused: a leading `/`/`\\`, a drive letter, a symlink cycle that cannot be resolved at all, or
    — after normalising `.`/`..` and following any local symlink — a path that lands outside the
    repo root. The one authoritative containment check; every other function trusts the `Path`
    this returns instead of re-deriving it from the raw entry string."""
    if entry_name.startswith(("/", "\\")) or _DRIVE_RE.match(entry_name):
        return None
    try:
        candidate = (repo_resolved / entry_name).resolve()
    except (OSError, RuntimeError):
        return None
    if not candidate.is_relative_to(repo_resolved):
        return None
    return candidate


def _blocked_by_non_directory(target_path: Path) -> Path | None:
    """The first ancestor of *target_path* that exists (or merely lexists — a broken or circular
    symlink included) but is not a usable directory — blocking any write under it — or None when
    nothing blocks. Pure inspection, no side effect, so `--dry-run` can call it and predict the
    exact failure a real write would hit."""
    for ancestor in target_path.parents:
        if not (ancestor.is_symlink() or ancestor.exists()):
            continue
        try:
            if ancestor.is_dir():
                return None
        except OSError:
            pass
        return ancestor
    return None


def source_entries(source: Path) -> Iterator[tuple[str, bytes]]:
    """Every file in *source* as (path, content) — *source* a zip archive or a plain directory."""
    if source.is_dir():
        for p in sorted(source.rglob("*")):
            if p.is_file():
                yield p.relative_to(source).as_posix(), p.read_bytes()
        return
    with zipfile.ZipFile(source) as zf:
        for info in zf.infolist():
            if not info.is_dir():
                yield info.filename, zf.read(info)


def read_manifest(source: Path) -> list[str] | None:
    """The card slugs listed in *source*'s top-level `EXPORTED_CARDS.txt` (one per line, blank
    lines ignored), or None when the source carries no such file. Same result whether *source*
    is a zip or an already-extracted directory."""
    for entry_name, content in source_entries(source):
        if _is_manifest_entry(entry_name):
            lines = content.decode("utf-8", errors="replace").splitlines()
            return [line.strip() for line in lines if line.strip()]
    return None


@dataclass(frozen=True)
class Outcome:
    path: str
    kind: str  # "created" | "applied" | "unchanged"
    classification: (
        str  # "code-changed" | "comments-only" | "not-python" | "unparseable"
    )


def apply_source(
    source: Path,
    repo: Path,
    *,
    dry_run: bool,
    agent_log_dir: Path | None = None,
    card_ids: list[str] | None = None,
) -> int:
    repo_resolved = repo.resolve()
    tracked = _tracked_files(repo_resolved)
    head_exists = _has_head(repo_resolved)
    outcomes: list[Outcome] = []
    skipped_traversal: list[str] = []
    skipped_dirty: list[str] = []
    skipped_conflict: list[str] = []
    skipped_error: list[tuple[str, str]] = []
    report_content: str | None = None

    for entry_name, content in source_entries(source):
        if _is_report_entry(entry_name):
            report_content = content.decode("utf-8", errors="replace")
            continue
        if _is_manifest_entry(entry_name):
            continue
        target_path = resolve_within_repo(repo_resolved, entry_name)
        if target_path is None:
            skipped_traversal.append(entry_name)
            continue
        target = target_path.relative_to(repo_resolved).as_posix()

        try:
            if target_path.is_dir():
                skipped_error.append(
                    (target, "a directory already exists at this path")
                )
                continue
            blocker = _blocked_by_non_directory(target_path)
            if blocker is not None:
                skipped_error.append(
                    (target, f"blocked by an existing non-directory file: {blocker}")
                )
                continue

            is_tracked = target in tracked
            exists_on_disk = target_path.is_file()

            if is_tracked and is_dirty(repo_resolved, target, head_exists=head_exists):
                skipped_dirty.append(target)
                continue
            if exists_on_disk and not is_tracked:
                old_bytes = target_path.read_bytes()
                if old_bytes != content:
                    skipped_conflict.append(target)
                    continue
                old_bytes_for_classify = old_bytes
            elif exists_on_disk:
                old_bytes_for_classify = target_path.read_bytes()
            else:
                old_bytes_for_classify = None

            if not target.endswith(".py"):
                classification = "not-python"
            elif old_bytes_for_classify is None:
                classification = "code-changed"
            else:
                classification = classify_change(old_bytes_for_classify, content)

            if old_bytes_for_classify is None:
                kind = "created"
            elif old_bytes_for_classify == content:
                kind = "unchanged"
            else:
                kind = "applied"

            if not dry_run and kind != "unchanged":
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(content)
            outcomes.append(Outcome(target, kind, classification))
        except OSError as exc:
            skipped_error.append((target, str(exc)))

    label = "would apply" if dry_run else "applied"
    for o in outcomes:
        if o.kind == "unchanged":
            print(f"·  unchanged: {o.path}")
            continue
        marker = "⚠️ " if o.classification == "code-changed" else "✅"
        tag = "new file" if o.kind == "created" else o.classification
        print(f"{marker} {label} [{tag}]: {o.path}")
    for path in skipped_traversal:
        print(f"🚫 refused (path traversal): {path}")
    for path in skipped_dirty:
        print(f"⚠️  skipped (uncommitted local changes already present): {path}")
    for path in skipped_conflict:
        print(
            f"⚠️  skipped (untracked local file already at this path, different content): {path}"
        )
    for path, reason in skipped_error:
        print(f"❌ skipped ({reason}): {path}")

    applied_count = sum(1 for o in outcomes if o.kind != "unchanged")
    code_changed = [
        o.path
        for o in outcomes
        if o.kind != "unchanged" and o.classification == "code-changed"
    ]
    unparseable = [
        o.path
        for o in outcomes
        if o.kind != "unchanged" and o.classification == "unparseable"
    ]
    print(
        f"\n{applied_count} {label}, {len(skipped_traversal)} traversal-refused, "
        f"{len(skipped_dirty)} dirty-skipped, {len(skipped_conflict)} conflict-skipped, "
        f"{len(skipped_error)} write-blocked, "
        f"{len(code_changed)} with actual code changes, {len(unparseable)} unparseable."
    )
    if code_changed:
        print("Review these by hand — not comment/docstring-only:")
        for path in code_changed:
            print(f"  - {path}")
    if unparseable:
        print("Could not parse as Python — review by hand:")
        for path in unparseable:
            print(f"  - {path}")
    if not dry_run and applied_count:
        print(
            "\nNothing committed. Next: run the target repo's lint/type-check/test/pre-commit loop "
            "(mechanical fixes auto-applied, anything touching logic or security stops for review), "
            "then review the diff and close the cards (reintegrate SKILL.md, Step 3)."
        )
    if report_content is not None:
        if dry_run:
            print(
                f"\n📝 REPORT.md present ({len(report_content)} chars) — dry-run, not written."
            )
        elif agent_log_dir is not None:
            name = _report_log_name(
                card_ids=card_ids or [],
                repo=repo_resolved,
                today=date.today().isoformat(),
            )
            log_path = agent_log_dir / name
            try:
                agent_log_dir.mkdir(parents=True, exist_ok=True)
                log_path.write_text(report_content, encoding="utf-8")
                print(f"\n📝 external agent's report written to {log_path}")
            except OSError as exc:
                print(f"\n⚠️  could not write REPORT.md to {log_path}: {exc}")
                print("--- REPORT.md ---")
                print(report_content)
        else:
            print("\n📝 REPORT.md (no --agent-log-dir given — not persisted):")
            print("--- REPORT.md ---")
            print(report_content)

    clean = not (
        skipped_traversal or skipped_dirty or skipped_conflict or skipped_error
    )
    return 0 if clean else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a zip file, or a directory")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--agent-log-dir",
        type=Path,
        default=None,
        help="where to write a REPORT.md found in the source, as a new dated entry",
    )
    parser.add_argument(
        "--card",
        action="append",
        default=[],
        dest="card_ids",
        help="card id this reintegration belongs to — used only to name the REPORT.md log entry",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"no such file or directory: {args.source}", file=sys.stderr)
        return 2
    if not (args.repo / ".git").exists():
        print(f"not a git repo: {args.repo}", file=sys.stderr)
        return 2

    try:
        return apply_source(
            args.source,
            args.repo.resolve(),
            dry_run=args.dry_run,
            agent_log_dir=args.agent_log_dir,
            card_ids=args.card_ids,
        )
    except zipfile.BadZipFile:
        print(f"not a valid zip file: {args.source}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
