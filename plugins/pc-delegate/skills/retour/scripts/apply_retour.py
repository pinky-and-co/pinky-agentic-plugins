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

Usage:
    python3 apply_retour.py <retour.zip-or-dir> [--repo PATH] [--dry-run]
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
from pathlib import Path

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


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


@dataclass(frozen=True)
class Outcome:
    path: str
    kind: str  # "created" | "applied" | "unchanged"
    classification: (
        str  # "code-changed" | "comments-only" | "not-python" | "unparseable"
    )


def apply_source(source: Path, repo: Path, *, dry_run: bool) -> int:
    repo_resolved = repo.resolve()
    tracked = _tracked_files(repo_resolved)
    head_exists = _has_head(repo_resolved)
    outcomes: list[Outcome] = []
    skipped_traversal: list[str] = []
    skipped_dirty: list[str] = []
    skipped_conflict: list[str] = []
    skipped_error: list[tuple[str, str]] = []

    for entry_name, content in source_entries(source):
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
            "then hand off to the `commit` skill (pc-git) — never an automatic commit/push/PR."
        )
    clean = not (
        skipped_traversal or skipped_dirty or skipped_conflict or skipped_error
    )
    return 0 if clean else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a zip file, or a directory")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"no such file or directory: {args.source}", file=sys.stderr)
        return 2
    if not (args.repo / ".git").exists():
        print(f"not a git repo: {args.repo}", file=sys.stderr)
        return 2

    try:
        return apply_source(args.source, args.repo.resolve(), dry_run=args.dry_run)
    except zipfile.BadZipFile:
        print(f"not a valid zip file: {args.source}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
