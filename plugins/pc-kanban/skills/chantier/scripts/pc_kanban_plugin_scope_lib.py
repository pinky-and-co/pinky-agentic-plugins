#!/usr/bin/env python3
"""Resolve which installed plugins apply to a given cwd, and
drive their activation via the official `claude plugin enable -s local` CLI.

Why not hand-edit settings.local.json: `claude plugin enable -s local <id>` is
the same write path Claude Code's own /plugin UI uses. It writes an
`enabledPlugins` entry correctly on the first call — no need to special-case
the "enabledPlugins key missing in the parent settings.json" bug some other
write paths hit, and no read-modify-write race with Claude Code's own writer.

Why installed_plugins.json is the source of truth for scope, not
settings.json's enabledPlugins: it is where `projectPath` actually lives per
plugin per scope (`~/.claude/plugins/installed_plugins.json`). We never write
to it — read-only, same posture as policy_lib.load_policies() toward
.claude/settings.pinky.json: parse, never mutate the source.

Exactly one settings.local.json is in play per session, not one per repo
touched — but that is the caller's doing, not the CLI's: `claude plugin
enable -s local` resolves "local" against the *current working directory*
of the process that runs it, exactly like `claude plugin install --scope
project`. `enable_plugin_local` therefore takes a `cwd` and the callers
pass the session's open directory (CLAUDE_PROJECT_DIR, fixed at launch)
every time, so every write lands in that one `<open>/.claude/
settings.local.json` and SessionEnd restores just that path. Run it from a
deeper cwd and you scatter a settings.local.json into each repo visited,
none of them cleaned up.

Ancestor-or-equal matching (not exact-path matching) is used for both the
SessionStart ascend and the CwdChanged descend: a plugin scoped to
`pinky-lib/pinky-ai` must still resolve when the actual cwd is
`pinky-lib/pinky-ai/tests`, not just at that exact path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

INSTALLED_PLUGINS_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
STATE_DIR = Path.home() / ".claude" / "pinky" / "plugin-scope"


# --- Reading the source of truth --------------------------------------------


def load_installed_plugins() -> dict:
    """Return the parsed installed_plugins.json, or {} if absent/unreadable.

    Read-only. Never write here — this file is Claude Code's own bookkeeping.
    """
    try:
        return json.loads(INSTALLED_PLUGINS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def is_ancestor_or_equal(parent: Path, child: Path) -> bool:
    """True if `child` is `parent` or a descendant of it (resolved, absolute)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _project_paths(install_entries: list[dict]) -> list[Path]:
    """The `projectPath` of every `scope: "project"` entry — the only scope
    that is path-restricted, so the only one a cwd descent looks at."""
    return [
        Path(entry["projectPath"])
        for entry in install_entries
        if entry.get("scope") == "project" and entry.get("projectPath")
    ]


def matching_plugin_ids(
    path: Path,
    installed: dict | None = None,
    *,
    open_path: Path | None = None,
) -> list[str]:
    """Return every `plugin@marketplace` id a descent into `path` should
    discover and enable: one whose `scope: "project"` install has a
    `projectPath` that is `path` or an ancestor of it.

    Two plugin-level exclusions (a plugin is dropped whole, not per entry):

    - A plugin with any `scope: "user"` entry is already active
      workspace-wide, so a descent never needs to enable it.
    - When `open_path` is given, a plugin whose `projectPath` is `open_path`
      or an ancestor of it already applied from session start — the
      SessionStart ascend handled it, re-enabling here is noise. The ascend
      itself omits `open_path`, because those are exactly what it wants.
    """
    installed = installed if installed is not None else load_installed_plugins()
    matches = []
    for plugin_id, install_entries in installed.get("plugins", {}).items():
        if any(entry.get("scope") == "user" for entry in install_entries):
            continue
        project_paths = _project_paths(install_entries)
        if open_path is not None and any(
            is_ancestor_or_equal(pp, open_path) for pp in project_paths
        ):
            continue
        if any(is_ancestor_or_equal(pp, path) for pp in project_paths):
            matches.append(plugin_id)
    return matches


def settings_local_plugin_ids(open_path: Path) -> list[str]:
    """Every plugin id present in `enabledPlugins` of the session's
    `settings.local.json` (the file `claude plugin enable -s local` writes),
    on or off, sorted. Absent or unreadable file → empty list.

    Presence, not truthiness: a plugin already listed there — even disabled —
    has been decided for this session, so a descent leaves it alone rather
    than flipping it back on.
    """
    settings_local = Path(open_path) / ".claude" / "settings.local.json"
    try:
        data = json.loads(settings_local.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    enabled = data.get("enabledPlugins", {})
    return sorted(enabled) if isinstance(enabled, dict) else []


def settings_json_plugin_ids(project_dir: Path) -> list[str]:
    """Every plugin id present in `enabledPlugins` of the repo's versioned
    `.claude/settings.json` (the file `claude plugin install --scope
    project` writes), on or off, sorted. Absent or unreadable file → empty
    list. Same presence-not-truthiness read as `settings_local_plugin_ids`,
    against the ascend's own file instead of the descent's."""
    settings_json = Path(project_dir) / ".claude" / "settings.json"
    try:
        data = json.loads(settings_json.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    enabled = data.get("enabledPlugins", {})
    return sorted(enabled) if isinstance(enabled, dict) else []


def project_scope_installed_ids(project_dir: Path) -> list[str]:
    """Plugin ids already installed at `scope: "project"` for exactly
    `project_dir` — not just an ancestor of it — per `installed_plugins.json`.
    A previous ascend, or `spawn-workers`' pre-enable, already ran `claude
    plugin install --scope project` here, so the ascend must not re-install
    (no-op, no nudge) — unlike `matching_plugin_ids`, which matches on
    ancestor-or-equal on purpose (a plugin project-scoped at a group root
    still needs enabling in a repo below it)."""
    installed = load_installed_plugins()
    resolved = Path(project_dir).resolve()
    matches = []
    for plugin_id, entries in installed.get("plugins", {}).items():
        for entry in entries:
            if entry.get("scope") != "project" or not entry.get("projectPath"):
                continue
            if Path(entry["projectPath"]).resolve() == resolved:
                matches.append(plugin_id)
                break
    return sorted(matches)


# --- Activation --------------------------------------------------------------


@dataclass(frozen=True)
class EnableResult:
    """Outcome of one `claude plugin enable -s local <id>` call.

    `already_enabled` splits the one non-zero exit the caller must not treat
    as a failure — the plugin is on already — from every real error.
    """

    success: bool
    already_enabled: bool
    stderr: str


# `claude plugin enable` refuse un plugin déjà actif ("... is already enabled
# at local scope") : ce n'est pas un échec pour nous, juste rien à faire.
_ALREADY_ENABLED_RE = re.compile(r"already enabled", re.IGNORECASE)


def enable_plugin_local(plugin_id: str, *, cwd: Path) -> EnableResult:
    """Run `claude plugin enable -s local <plugin_id>` from `cwd` — "local"
    resolves against that directory, so it decides which `.claude/
    settings.local.json` gets the `enabledPlugins` entry. Callers pass the
    session's open directory so every write lands in the same one file (see
    the module docstring). Same `cwd=` pattern as `pinky`'s own
    `plugin_install.enable_at_project`."""
    try:
        result = subprocess.run(
            ["claude", "plugin", "enable", "-s", "local", plugin_id],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EnableResult(False, False, str(exc))
    stderr = result.stderr.strip()
    if result.returncode == 0:
        return EnableResult(True, False, stderr)
    return EnableResult(False, bool(_ALREADY_ENABLED_RE.search(stderr)), stderr)


def install_plugin_project(plugin_id: str, *, cwd: Path) -> EnableResult:
    """Run `claude plugin install <plugin_id> --scope project --yes` from
    `cwd` — the exact call `spawn-workers`' `enable_project_plugins` makes
    before opening a worker, reused here for the `SessionStart` ascend when
    it opens directly in the repo: it writes the versioned `cwd/.claude/
    settings.json`, not the ephemeral `settings.local.json` a local enable
    would. No retry on failure or timeout — same bounded posture as
    `enable_plugin_local`."""
    try:
        result = subprocess.run(
            ["claude", "plugin", "install", plugin_id, "--scope", "project", "--yes"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EnableResult(False, False, str(exc))
    stderr = result.stderr.strip()
    if result.returncode == 0:
        return EnableResult(True, False, stderr)
    return EnableResult(False, bool(_ALREADY_ENABLED_RE.search(stderr)), stderr)


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.json"


def backup_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.settings.local.json.bak"


def load_state(session_id: str) -> dict:
    """{"activated": [...plugin_ids], "settings_local_path": str|None} — the
    shared per-directory backup (see `backup_settings_local_shared`) lives
    outside this per-session file, keyed by directory, not session."""
    try:
        state = json.loads(state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    state.setdefault("activated", [])
    state.setdefault("settings_local_path", None)
    return state


def save_state(session_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(session_id).write_text(json.dumps(state))


def clear_state(session_id: str) -> None:
    try:
        state_path(session_id).unlink()
    except FileNotFoundError:
        pass


# A session is alive while the `claude` process recorded at SessionStart (`os.getppid()`) is:
# a flock would release with each short-lived hook process. A reused PID can delay a crashed
# session's purge until that unrelated process exits.


def _dir_key(settings_local: Path) -> str:
    """Filesystem-safe key for `settings_local`'s resolved path, shared by
    every session open in that directory — the backup and the live-session
    registry below are both keyed on this, not on any one session_id."""
    return hashlib.sha256(str(Path(settings_local).resolve()).encode()).hexdigest()


def _shared_dir(key: str) -> Path:
    return STATE_DIR / "shared" / key


def _sessions_dir(key: str) -> Path:
    return _shared_dir(key) / "sessions"


def _is_alive_posix(pid: int) -> bool:
    """POSIX liveness via the null-signal probe (`os.kill(pid, 0)` sends no
    actual signal, just checks the kernel's process table)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal — still alive
    return True


def _is_alive_windows(pid: int) -> bool:
    """Windows liveness — `os.kill(pid, sig)` is not a probe there: CPython
    special-cases only `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`, every other signal
    (0 included) goes to `TerminateProcess(handle, sig)`, so `os.kill(pid, 0)`
    would kill the target instead of checking it. Opens a query-only handle
    and reads the exit code instead — `STILL_ACTIVE` (259) means running; a
    failed `OpenProcess` means the PID no longer exists."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.pointer(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _is_alive(pid: int) -> bool:
    """Whether `pid` still names a running process — dispatches to the
    platform-correct probe, since a null `os.kill` signal means "check" on
    POSIX and "terminate" on Windows."""
    if sys.platform == "win32":
        return _is_alive_windows(pid)
    return _is_alive_posix(pid)


def _purge_dead_sessions(key: str) -> list[Path]:
    """Delete every session-pid file under `key` whose recorded PID is no
    longer alive. Returns the remaining (still-alive) session-pid file paths."""
    sessions_dir = _sessions_dir(key)
    if not sessions_dir.is_dir():
        return []
    alive = []
    for f in sessions_dir.glob("*.pid"):
        try:
            pid = int(f.read_text().strip())
        except (OSError, ValueError):
            f.unlink(missing_ok=True)
            continue
        if _is_alive(pid):
            alive.append(f)
        else:
            f.unlink(missing_ok=True)
    return alive


def backup_settings_local_shared(
    settings_local: Path, session_id: str, ppid: int
) -> None:
    """Register this session as active on `settings_local`'s resolved
    directory, and capture the shared original-content backup the first time
    any session claims it — an atomic create-if-absent on the meta file
    (`os.O_CREAT | os.O_EXCL`, a single syscall, no lock to hold across
    processes) decides "first" even if two sessions start at once. A later
    session — concurrent, or arriving after every previous one crashed
    without `SessionEnd` — joins the existing backup instead of recapturing
    it, so a crashed session's own edits never get mistaken for the original.
    """
    key = _dir_key(settings_local)
    sessions_dir = _sessions_dir(key)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{session_id}.pid").write_text(str(ppid))

    shared = _shared_dir(key)
    meta_path = shared / "meta.json"
    try:
        fd = os.open(str(meta_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return  # a backup already exists for this directory — join it
    try:
        if settings_local.is_file():
            shutil.copy2(settings_local, shared / "backup.settings.local.json")
            os.write(fd, json.dumps({"existed": True}).encode())
        else:
            os.write(fd, json.dumps({"existed": False}).encode())
    finally:
        os.close(fd)


def restore_settings_local_shared(settings_local: Path, session_id: str) -> None:
    """Drop this session from its directory's live-session registry; once no
    session remains there (this one, plus any other found still alive after
    purging dead entries), restore `settings_local` to the shared backup and
    clear the directory's shared state so a later open starts fresh."""
    key = _dir_key(settings_local)
    (_sessions_dir(key) / f"{session_id}.pid").unlink(missing_ok=True)
    if _purge_dead_sessions(key):
        return  # another session is still open here — don't restore yet

    shared = _shared_dir(key)
    try:
        meta = json.loads((shared / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        meta = None
    if meta is not None:
        if meta.get("existed"):
            shutil.copy2(shared / "backup.settings.local.json", settings_local)
        else:
            settings_local.unlink(missing_ok=True)
    shutil.rmtree(shared, ignore_errors=True)
