"""Compute the export subtree for a set of touched files — the touched files themselves plus the
modules they directly import (depth 1), resolved on disk, never transitively.

Two resolution strategies, tried per touched file:

- **Package strategy (`grimp`)** — the touched file sits under a directory chain that carries
  `__init__.py` all the way up. `grimp.build_graph` on the top package name returns direct imports
  precisely, including relative imports and re-exports.
- **Flat-directory fallback (AST)** — the touched file has no `__init__.py` in its own directory (a
  standalone script, common in this workspace's `scripts/`/`skills/*/scripts/` layout). A plain
  `ast.walk` over its `Import`/`ImportFrom` nodes, matched against sibling `.py` files in the same
  directory, covers the same "direct import, same repo" case grimp cannot see without a package.

Both strategies stay strictly depth-1: neither recurses into what the direct imports themselves
import.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path


class SubtreeError(Exception):
    pass


@dataclass(frozen=True)
class _PackageRoot:
    sys_path_root: Path
    top_package: str
    dotted_module: str


def _package_root(repo: Path, file_path: Path) -> _PackageRoot | None:
    """The top-most package chain *file_path* belongs to, or None when its own directory carries
    no `__init__.py` — not part of an importable package. The climb never goes past *repo* itself
    (a package rooted above it is not this export's business), but *repo* can legitimately BE the
    top package (a single-package library repo) — `sys_path_root` is then `repo`'s own parent, one
    level above the package, never the package directory itself."""
    if not (file_path.parent / "__init__.py").is_file():
        return None
    chain: list[str] = []
    top = file_path.parent
    while True:
        chain.insert(0, top.name)
        if top == repo:
            break
        parent = top.parent
        if (
            parent == top
            or not (parent / "__init__.py").is_file()
            or not parent.is_relative_to(repo)
        ):
            break
        top = parent
    module_parts = list(chain)
    if file_path.stem != "__init__":
        module_parts.append(file_path.stem)
    return _PackageRoot(top.parent, chain[0], ".".join(module_parts))


def _via_grimp(root: _PackageRoot) -> set[Path]:
    import grimp

    sys.path.insert(0, str(root.sys_path_root))
    try:
        # cache_dir=None : appel interactif ponctuel, jamais une boucle chaude — sans ça, le cache
        # disque de grimp disperserait un `.grimp_cache/` selon le cwd de l'appelant du moment.
        graph = grimp.build_graph(root.top_package, cache_dir=None)
    finally:
        sys.path.remove(str(root.sys_path_root))
    imported = graph.find_modules_directly_imported_by(root.dotted_module)

    results: set[Path] = set()
    for module in imported:
        candidate = root.sys_path_root / Path(*module.split(".")).with_suffix(".py")
        if candidate.is_file():
            results.add(candidate)
            continue
        candidate_pkg = root.sys_path_root / Path(*module.split(".")) / "__init__.py"
        if candidate_pkg.is_file():
            results.add(candidate_pkg)
    return results


def _via_ast_siblings(file_path: Path) -> set[Path]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    top_level_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top_level_names.add(node.module.split(".")[0])
    top_level_names -= sys.stdlib_module_names

    results: set[Path] = set()
    for name in top_level_names:
        sibling = file_path.parent / f"{name}.py"
        if sibling.is_file() and sibling != file_path:
            results.add(sibling)
    return results


def compute_subtree(repo: Path, touched_files: list[str]) -> list[str]:
    """*touched_files* (repo-relative) plus every module each one directly imports, resolved on
    disk and de-duplicated — repo-relative POSIX paths, sorted. Raises `SubtreeError` if *repo*
    does not exist or a touched entry escapes it."""
    if not repo.is_dir():
        raise SubtreeError(f"not a directory: {repo}")
    repo_resolved = repo.resolve()

    all_paths: set[Path] = set()
    for rel in touched_files:
        file_path = (repo_resolved / rel).resolve()
        if not file_path.is_relative_to(repo_resolved):
            raise SubtreeError(f"touched file escapes repo root: {rel}")
        if not file_path.is_file():
            raise SubtreeError(f"not a file: {rel}")
        all_paths.add(file_path)

        if not rel.endswith(".py"):
            continue
        root = _package_root(repo_resolved, file_path)
        if root is not None:
            try:
                deps = _via_grimp(root)
            except Exception as exc:
                raise SubtreeError(f"grimp failed to resolve {rel}: {exc}") from exc
        else:
            deps = _via_ast_siblings(file_path)
        for dep in deps:
            if dep.is_relative_to(repo_resolved):
                all_paths.add(dep)

    return sorted(p.relative_to(repo_resolved).as_posix() for p in all_paths)
