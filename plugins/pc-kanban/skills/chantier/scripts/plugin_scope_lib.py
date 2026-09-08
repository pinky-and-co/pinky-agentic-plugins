#!/usr/bin/env python3
"""plugin_scope_lib — resolve which installed plugins apply to a given cwd, and
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
touched: `claude plugin enable -s local` resolves "local" against
CLAUDE_PROJECT_DIR, fixed at session launch and inherited unchanged by every
subprocess a hook spawns — the same value inject_policies.py already reads
via `os.environ.get("CLAUDE_PROJECT_DIR", ...)`. It is never recomputed from
a deeper cwd mid-session, so a single backup/restore at that one path is
correct — no per-repo backup dict needed.

Ancestor-or-equal matching (not exact-path matching) is used for both the
SessionStart ascend and the CwdChanged descend: a plugin scoped to
`pinky-lib/pinky-ai` must still resolve when the actual cwd is
`pinky-lib/pinky-ai/tests`, not just at that exact path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

INSTALLED_PLUGINS_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
STATE_DIR = Path.home() / ".claude" / "pinky-plugin-scope"


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


def matching_plugin_ids(path: Path, installed: dict | None = None) -> list[str]:
    """Return every `plugin@marketplace` id whose *project*-scoped install
    (any entry) has a `projectPath` that is `path` or an ancestor of it.

    `scope: "user"` entries are skipped on purpose: they are already active
    workspace-wide, never subject to the cwd-gating problem this hook exists
    to work around. Only `scope: "project"` entries are path-restricted.
    """
    installed = installed if installed is not None else load_installed_plugins()
    matches = []
    for plugin_id, install_entries in installed.get("plugins", {}).items():
        for entry in install_entries:
            if entry.get("scope") != "project":
                continue
            project_path = entry.get("projectPath")
            if project_path and is_ancestor_or_equal(Path(project_path), path):
                matches.append(plugin_id)
                break  # one matching entry is enough for this plugin
    return matches


# --- Activation --------------------------------------------------------------


@dataclass(frozen=True)
class EnableResult:
    """Outcome of one `claude plugin enable -s local <id>` call."""

    success: bool
    stderr: str


def enable_plugin_local(plugin_id: str) -> EnableResult:
    """Run `claude plugin enable -s local <plugin_id>`, inheriting the calling
    process's environment (including CLAUDE_PROJECT_DIR) unchanged — that
    inherited value, not any explicit cwd, is what "local" resolves against."""
    try:
        result = subprocess.run(
            ["claude", "plugin", "enable", "-s", "local", plugin_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return EnableResult(result.returncode == 0, result.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EnableResult(False, str(exc))


# --- Per-session state (which plugin ids we've already activated) -----------
# Kept outside any repo's .claude/ on purpose — this is ephemeral session
# bookkeeping, not project configuration, and never needs a .gitignore entry.


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.json"


def backup_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.settings.local.json.bak"


def load_state(session_id: str) -> dict:
    """{"activated": [...plugin_ids], "settings_local_path": str|None,
    "existed": bool|None} — the backup's actual content lives in its own
    file (see backup_path), not embedded here as a string."""
    try:
        state = json.loads(state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    state.setdefault("activated", [])
    state.setdefault("settings_local_path", None)
    state.setdefault("existed", None)
    return state


def save_state(session_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(session_id).write_text(json.dumps(state))


def clear_state(session_id: str) -> None:
    try:
        state_path(session_id).unlink()
    except FileNotFoundError:
        pass
    try:
        backup_path(session_id).unlink()
    except FileNotFoundError:
        pass


# --- Backup / restore of the single settings.local.json for this session ---
# A real file copy, not a string embedded in the state JSON: settings.local.json
# stays inspectable as itself on disk, and there's no JSON-in-JSON encoding to
# get wrong or to make the state file balloon if settings.local.json ever does.


def backup_settings_local(settings_local: Path, session_id: str) -> bool:
    """Copy settings_local to its backup file if it exists. Returns whether
    the original existed at all — the one bit `restore_settings_local` needs
    that a file copy alone can't carry (a missing backup file is ambiguous
    between "never existed" and "not yet backed up")."""
    if settings_local.is_file():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings_local, backup_path(session_id))
        return True
    return False


def restore_settings_local(
    settings_local: Path, session_id: str, existed: bool | None
) -> None:
    """Put settings.local.json back exactly how backup_settings_local found it."""
    if existed is None:
        return  # no backup was ever taken this session — nothing to restore
    if existed:
        shutil.copy2(backup_path(session_id), settings_local)
    else:
        try:
            settings_local.unlink()
        except FileNotFoundError:
            pass
