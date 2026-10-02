#!/usr/bin/env python3
"""Shared resolution of memory directories and directory levels for the pc-memory plugin.

Never declared in `hooks.json`: a pure dependency imported by `memory-context/memory_context.py`
(SessionStart), `memory-write/memory_write_guard.py` (PreToolUse), `memory-count/memory_count.py`
(PostToolUse) and the scripts of the `reconcile` skill, so all resolve a level's memory directory
the same way, and read and write the consultation ledger the same way.

A level is the git root (the main repo's root from inside a worktree) found below the home
directory, else the working directory, then each ancestor up to the home directory, excluded.

A level's memory directory is the one `autoMemoryDirectory` points to (level
`.claude/settings.local.json`, then level `.claude/settings.json`, then user `settings.json` in
the config directory). Without a declaration, the default per-project silo under
`<config dir>/projects/` is used. The config directory is `CLAUDE_CONFIG_DIR`, else `~/.claude`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # no flock on this platform
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "HAS_FLOCK",
    "INDEX_NAME",
    "LEDGER_NAME",
    "LOCK_NAME",
    "LOCK_TIMEOUT",
    "MARKER_MAX_AGE",
    "MARKER_NAME",
    "Level",
    "clean_ledger",
    "clean_text",
    "collect_levels",
    "config_dir",
    "configured_memory_dir",
    "declared_memory_dirs",
    "encode",
    "exclusive_lock",
    "find_memory_dir",
    "home_dir",
    "is_default_silo",
    "is_default_silo_root",
    "is_memory_dir",
    "level_root",
    "level_roots",
    "link_targets",
    "load_ledger",
    "marker_active",
    "memory_dir_of",
    "note_age_days",
    "read_last_review",
    "short",
    "spinner_tips_enabled",
    "write_ledger_atomically",
]

INDEX_NAME = "MEMORY.md"
LEDGER_NAME = ".pc-memory-ledger.json"
LOCK_NAME = ".pc-memory-ledger.lock"
MARKER_NAME = ".pc-memory-reconciling"
MARKER_MAX_AGE = 600
HAS_FLOCK = fcntl is not None
LOCK_TIMEOUT = 1.0
SECONDS_PER_DAY = 86400
FRONTMATTER_BYTES = 4096
_LOCK_POLL = 0.01
_UNRESOLVABLE = (OSError, ValueError, RuntimeError)
_GITDIR = re.compile(r"^gitdir:\s*(.+?)\s*$")
_MODIFIED = re.compile(r"^modified:\s*(.+?)\s*$")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LINK_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)")
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


def find_memory_dir(
    level: Path, trust_shared_project_settings: bool = True
) -> Path | None:
    """Memory directory of a level when it has an index, None otherwise.

    `trust_shared_project_settings` is passed to `memory_dir_of`.
    """
    candidate = memory_dir_of(level, trust_shared_project_settings)
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


def collect_levels(
    cwd: Path, trust_shared_project_settings: bool = True
) -> list[Level]:
    """Levels that have a memory index, nearest first, each memory directory once.

    `trust_shared_project_settings` is passed to `find_memory_dir`: false resolves the directories
    a writer may use.
    """
    levels: list[Level] = []
    seen: set[Path] = set()
    for root in level_roots(cwd):
        try:
            memory_dir = find_memory_dir(root, trust_shared_project_settings)
            key = memory_dir.resolve() if memory_dir is not None else None
        except OSError:
            continue
        if memory_dir is None or key is None:
            continue
        if key in seen:
            continue
        seen.add(key)
        levels.append(Level(root=root, memory_dir=memory_dir))
    return levels


def _frontmatter_modified(note: Path) -> float | None:
    """Timestamp of the `modified` frontmatter field of a note, None when absent or unparsable."""
    try:
        with note.open(encoding="utf-8", errors="replace") as handle:
            head = handle.read(FRONTMATTER_BYTES).splitlines()
    except OSError:
        return None
    if not head or head[0].strip() != "---":
        return None
    for line in head[1:]:
        if line.strip() == "---":
            return None
        match = _MODIFIED.match(line)
        if match is None:
            continue
        raw = match.group(1).strip("\"'")
        try:
            if raw[-1:] in ("Z", "z"):
                raw = raw[:-1] + "+00:00"
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None
    return None


def note_age_days(note: Path, now: float) -> int:
    """Age of a note in days: frontmatter `modified`, else file mtime. Never negative."""
    stamp = _frontmatter_modified(note)
    if stamp is None:
        stamp = note.stat().st_mtime
    return max(0, int((now - stamp) // SECONDS_PER_DAY))


def link_targets(line: str) -> list[str]:
    """Targets of the markdown links of one index line, `./` and `#fragment` removed.

    A target is returned as written and, when it differs, percent-decoded, so a note whose name
    holds a space matches whichever way the index spells it. A link with a scheme (`https://`)
    is not a file and is left out.
    """
    from urllib.parse import unquote  # only an index reader needs it

    out: list[str] = []
    for raw in _LINK_TARGET.findall(line):
        if "://" in raw or raw.startswith("mailto:"):
            continue
        for candidate in (raw, unquote(raw)):
            target = candidate.partition("#")[0]
            while target.startswith("./"):
                target = target[2:]
            if target and target not in out:
                out.append(target)
    return out


def declared_memory_dirs(cwd: Path) -> list[Path]:
    """Memory directory of each level of `cwd`, as given and resolved.

    The level's shared `.claude/settings.json`, which a cloned repo controls, is not honoured: a
    caller that writes into the directory is not redirected by it.
    """
    out: list[Path] = []
    for root in level_roots(cwd):
        try:
            candidate = memory_dir_of(root, trust_shared_project_settings=False)
            out.extend((candidate, candidate.resolve()))
        except _UNRESOLVABLE:
            continue
    return out


def is_memory_dir(directory: Path, declared: list[Path]) -> bool:
    """True when `directory` is a default silo, or one of the `declared` level directories."""
    return is_default_silo_root(directory) or directory in declared


def clean_ledger(raw: object) -> dict[str, object]:
    """Ledger rebuilt from decoded JSON, keeping every note entry and unknown key that is usable.

    Args:
        raw: Whatever the ledger file decoded to.

    Returns:
        A ledger with a `notes` object and a `last_review` key.
    """
    data = raw if isinstance(raw, dict) else {}
    notes: dict[str, object] = {}
    stored = data.get("notes")
    for name, entry in (stored if isinstance(stored, dict) else {}).items():
        if not isinstance(entry, dict):
            continue
        sessions = entry.get("sessions")
        count = sessions if isinstance(sessions, int) and sessions > 0 else 0
        notes[name] = {**entry, "sessions": count}
    extra = {k: v for k, v in data.items() if k not in ("notes", "last_review")}
    return {"notes": notes, "last_review": data.get("last_review"), **extra}


def load_ledger(ledger_file: Path, source: str = "memory_count") -> dict[str, object]:
    """Ledger from disk: empty when absent, rebuilt from what is usable when corrupt.

    A ledger that cannot be decoded or parsed is moved to `<name>.corrupt` first, so the rebuilt
    one never overwrites it, and `source` names the caller in the message on stderr. Any other
    read error (a directory, no permission) propagates.

    Args:
        ledger_file: Path of the ledger.
        source: Name written before the message on stderr.

    Returns:
        A ledger as returned by `clean_ledger`.
    """
    try:
        raw = ledger_file.read_bytes()
    except FileNotFoundError:
        return clean_ledger(None)
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return clean_ledger(data)
    except ValueError as exc:
        sys.stderr.write(f"{source}: corrupt ledger {ledger_file.name}: {exc}\n")
        os.replace(ledger_file, ledger_file.with_name(ledger_file.name + ".corrupt"))
        return clean_ledger(None)


def read_last_review(memory_dir: Path) -> date | None:
    """`last_review` of a memory directory's ledger, read-only: it never renames or writes.

    Args:
        memory_dir: The memory directory holding the ledger.

    Returns:
        The date when the ledger holds a valid `YYYY-MM-DD` string, None for a missing, unreadable
        or corrupt ledger and for any other value.
    """
    ledger_file = memory_dir / LEDGER_NAME
    try:
        if not ledger_file.is_file():  # a FIFO would block the read
            return None
        data = json.loads(ledger_file.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    value = data.get("last_review") if isinstance(data, dict) else None
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def write_ledger_atomically(ledger_file: Path, ledger: dict[str, object]) -> None:
    """Replace the ledger with `ledger` through a temp file in the same directory."""
    import tempfile  # only a counted read gets here: the common read pays no import

    fd, tmp = tempfile.mkstemp(dir=ledger_file.parent, prefix=".pc-memory-ledger.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, ledger_file)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


@contextlib.contextmanager
def exclusive_lock(
    memory_dir: Path,
    timeout: float = LOCK_TIMEOUT,
    message: str = "ledger lock still held, nothing counted",
) -> Iterator[None]:
    """Hold an exclusive `flock` on the ledger's lock file for the duration of the block.

    The lock is polled without blocking; after `timeout` seconds a `TimeoutError` carrying
    `message` is raised.
    """
    if fcntl is None:
        raise OSError("flock is not available on this platform")
    with open(memory_dir / LOCK_NAME, "a", encoding="utf-8") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(message) from None
                time.sleep(_LOCK_POLL)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def marker_active(directory: Path, now: float | None = None) -> bool:
    """True when `directory` holds a reconcile marker that is a regular file younger than `MARKER_MAX_AGE`.

    An older marker, left by a run that died, suspends nothing. A symlink or any other kind of
    entry is not a marker.
    """
    try:
        info = os.lstat(directory / MARKER_NAME)
    except OSError:
        return False
    age = (time.time() if now is None else now) - info.st_mtime
    return stat.S_ISREG(info.st_mode) and age < MARKER_MAX_AGE
