#!/usr/bin/env python3
"""Shared resolution of memory directories and directory levels for the pc-memory hooks.

Never declared in `hooks.json`: a pure dependency imported by `memory-context/memory_context.py`
(SessionStart), `memory-write/memory_write_guard.py` (PreToolUse) and `memory-count/memory_count.py`
(PostToolUse), so all resolve a level's memory directory the same way, and name the consultation
ledger files the same way.

A level is the git root (the main repo's root from inside a worktree) found below the home
directory, else the working directory, then each ancestor up to the home directory, excluded.

A level's memory directory is the one `autoMemoryDirectory` points to (level
`.claude/settings.local.json`, then level `.claude/settings.json`, then user `settings.json` in
the config directory). Without a declaration, the default per-project silo under
`<config dir>/projects/` is used. The config directory is `CLAUDE_CONFIG_DIR`, else `~/.claude`.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "INDEX_NAME",
    "LEDGER_NAME",
    "LOCK_NAME",
    "MARKER_NAME",
    "Level",
    "clean_text",
    "config_dir",
    "configured_memory_dir",
    "encode",
    "find_memory_dir",
    "home_dir",
    "is_default_silo",
    "is_default_silo_root",
    "level_root",
    "level_roots",
    "memory_dir_of",
    "short",
    "spinner_tips_enabled",
]

INDEX_NAME = "MEMORY.md"
LEDGER_NAME = ".pc-memory-ledger.json"
LOCK_NAME = ".pc-memory-ledger.lock"
MARKER_NAME = ".pc-memory-reconciling"
_GITDIR = re.compile(r"^gitdir:\s*(.+?)\s*$")
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


@dataclass(frozen=True)
class Level:
    """A directory level that owns a memory index."""

    root: Path
    memory_dir: Path


def home_dir() -> Path:
    """Resolved home directory, the upper bound of the level walk."""
    return Path.home().resolve()


def config_dir() -> Path:
    """Claude Code's config directory: `CLAUDE_CONFIG_DIR` when set, else `~/.claude`."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / ".claude"


def encode(path: Path) -> str:
    """Name of the Claude Code project folder for `path`: every non-alphanumeric becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _settings_sources(
    level: Path, trust_shared_project_settings: bool = True
) -> list[tuple[Path, Path]]:
    """Settings files by priority, each with the base a relative path resolves from.

    Level `.claude/settings.local.json`, level `.claude/settings.json` (left out when
    `trust_shared_project_settings` is false), then the user `settings.json` in the config
    directory.
    """
    sources = [(level / ".claude" / "settings.local.json", level)]
    if trust_shared_project_settings:
        sources.append((level / ".claude" / "settings.json", level))
    sources.append((config_dir() / "settings.json", config_dir()))
    return sources


def _read_value(settings_file: Path, key: str) -> object:
    """Value of `key` in one settings file, None when absent or unreadable."""
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get(key) if isinstance(data, dict) else None


def configured_memory_dir(
    level: Path, trust_shared_project_settings: bool = True
) -> Path | None:
    """Memory directory redirected through `autoMemoryDirectory`, None when no settings set it.

    Level settings win over user settings. A relative path resolves from the level for level
    settings and from the config directory for the user settings.

    Args:
        level: The level's root directory.
        trust_shared_project_settings: False ignores the level's `.claude/settings.json`, the file a
            cloned repo ships, so a caller that writes into the directory is not redirected by it.

    Returns:
        The redirected directory, or None.
    """
    for settings_file, base in _settings_sources(level, trust_shared_project_settings):
        value = _read_value(settings_file, "autoMemoryDirectory")
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(os.path.expanduser(value.strip()))
        return candidate if candidate.is_absolute() else base / candidate
    return None


def spinner_tips_enabled(level: Path) -> bool:
    """Value of the `spinnerTipsEnabled` setting, True when no settings file sets a boolean.

    Same priority as `autoMemoryDirectory`: the first file holding a boolean wins. A value that
    is not a boolean is ignored, as is an unreadable file.

    Args:
        level: The level's root directory.

    Returns:
        False only when the winning setting is the boolean false.
    """
    for settings_file, _ in _settings_sources(level):
        value = _read_value(settings_file, "spinnerTipsEnabled")
        if isinstance(value, bool):
            return value
    return True


def memory_dir_of(level: Path, trust_shared_project_settings: bool = True) -> Path:
    """Memory directory of a level, whether or not it exists yet.

    A declared `autoMemoryDirectory` is the only candidate; the default silo is used otherwise.
    `trust_shared_project_settings` is passed to `configured_memory_dir`.
    """
    declared = configured_memory_dir(level, trust_shared_project_settings)
    if declared is not None:
        return declared
    return config_dir() / "projects" / encode(level) / "memory"


def _silo_parts(path: Path) -> tuple[str, ...] | None:
    """Parts of `path` below `<config dir>`, when it is inside `projects/<project>/memory`."""
    for base in {config_dir(), config_dir().resolve()}:
        try:
            parts = path.relative_to(base).parts
        except ValueError:
            continue
        if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "memory":
            return parts
    return None


def is_default_silo(path: Path) -> bool:
    """True when `path` is inside `<config dir>/projects/<project>/memory`, whatever the project."""
    return _silo_parts(path) is not None


def is_default_silo_root(path: Path) -> bool:
    """True when `path` is exactly `<config dir>/projects/<project>/memory`."""
    parts = _silo_parts(path)
    return parts is not None and len(parts) == 3


def find_memory_dir(level: Path) -> Path | None:
    """Memory directory of a level when it has an index, None otherwise."""
    candidate = memory_dir_of(level)
    return candidate if (candidate / INDEX_NAME).is_file() else None


def _main_repo_root(git_file: Path, fallback: Path) -> Path:
    """Root of the main repo a worktree's `.git` file points to, `fallback` for anything else.

    A submodule's `.git` file points to `<repo>/.git/modules/<name>`, not to a worktree entry, so
    it keeps `fallback`.
    """
    try:
        text = git_file.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback
    match = _GITDIR.match(text)
    if match is None:
        return fallback
    gitdir = (git_file.parent / match.group(1)).resolve()
    common = gitdir.parent.parent
    if gitdir.parent.name != "worktrees" or common.name != ".git":
        return fallback
    return common.parent


def level_root(cwd: Path) -> Path:
    """Git root below the home directory (memory is attached to it), else `cwd` itself.

    From inside a worktree the root is the main repo's, whose path names the memory silo. The walk
    stops before the home directory, so a `.git` kept in `$HOME` never captures every session.
    """
    if home_dir() not in cwd.parents:
        return cwd
    for candidate in (cwd, *cwd.parents):
        if candidate == home_dir():
            break
        marker = candidate / ".git"
        if marker.is_dir():
            return candidate
        if marker.is_file():
            return _main_repo_root(marker, candidate)
    return cwd


def level_roots(cwd: Path) -> list[Path]:
    """Current level, then each ancestor up to the home directory (excluded)."""
    home = home_dir()
    start = level_root(cwd)
    roots = [start]
    current = start
    while True:
        parent = current.parent
        if parent == current or parent == home or home not in parent.parents:
            break
        roots.append(parent)
        current = parent
    return roots


def clean_text(text: str) -> str:
    """`text` with tabs as spaces, and every control, format and line-separator character removed.

    Covers the Unicode categories Cc (C0, DEL, C1, ESC included), Cf (zero-width, bidi overrides),
    Zl and Zp (U+2028/2029), plus the whole U+2060-U+206F block, which holds unassigned code
    points.
    """
    return "".join(
        ch
        for ch in text.replace("\t", " ")
        if unicodedata.category(ch) not in _STRIPPED_CATEGORIES
        and not "\u2060" <= ch <= "\u206f"
    )


def short(path: Path) -> str:
    """`path` with the home directory abbreviated to `~`."""
    text = str(path)
    for home in (str(home_dir()), str(Path.home())):
        if text == home or text.startswith(home + os.sep):
            return "~" + text[len(home) :]
    return text
