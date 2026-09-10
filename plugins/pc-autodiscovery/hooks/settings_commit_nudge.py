#!/usr/bin/env python3
"""settings_commit_nudge — SessionStart hook.

`spawn-workers` (and a manual `claude plugin enable -s project`) writes a
repo's plugin activation into its *versioned* `.claude/settings.json`. That
change is expected and belongs in git — but a worker session, seeing a file
it did not touch, tends to leave it: "pre-existing, not mine". This hook
tells the session otherwise, but only when it is unambiguous: the working
tree's single change is `enabledPlugins` entries in `.claude/settings.json`
and nothing else.

Silent (no `additionalContext`) when the session is not in a git repo, the
tree is clean, another tracked file is also dirty, or the settings.json diff
touches any key other than `enabledPlugins` — anything where "just commit
it" would be wrong advice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SETTINGS_REL = ".claude/settings.json"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def repo_root(start: Path) -> Path | None:
    """The git worktree root containing `start`, or None if there is none."""
    proc = _git(start, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def _dirty_tracked(repo: Path) -> list[str]:
    """Repo-relative paths of every tracked file with a staged or unstaged
    change (modified / added / deleted / renamed). Untracked files excluded —
    a stray scratch file is not "a larger diff"."""
    proc = _git(repo, "status", "--porcelain")
    if proc.returncode != 0:
        return []
    paths = []
    for line in proc.stdout.splitlines():
        if not line or line.startswith("??"):
            continue
        # porcelain v1: 2 status chars, a space, then the path (or "old -> new")
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def _committed_json(repo: Path, rel: str) -> dict | None:
    """`HEAD:<rel>` parsed as JSON, or None when the path is not in HEAD."""
    proc = _git(repo, "show", f"HEAD:{rel}")
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _working_json(repo: Path, rel: str) -> dict | None:
    try:
        return json.loads((repo / rel).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _only_enabled_plugins_changed(before: dict | None, after: dict) -> bool:
    """True when `after` differs from `before` only within `enabledPlugins`
    (a brand-new file counts when `enabledPlugins` is its sole key)."""
    if before is None:
        return set(after) <= {"enabledPlugins"} and "enabledPlugins" in after
    other_keys = (set(before) | set(after)) - {"enabledPlugins"}
    if any(before.get(k) != after.get(k) for k in other_keys):
        return False
    return before.get("enabledPlugins") != after.get("enabledPlugins")


def nudge(session_dir: Path) -> str | None:
    """The `additionalContext` string when a plugin-activation-only settings
    change is the sole thing uncommitted in the session's repo — else None."""
    repo = repo_root(session_dir)
    if repo is None:
        return None
    if _dirty_tracked(repo) != [SETTINGS_REL]:
        return None
    after = _working_json(repo, SETTINGS_REL)
    if after is None:
        return None
    if not _only_enabled_plugins_changed(_committed_json(repo, SETTINGS_REL), after):
        return None
    return (
        f"`{SETTINGS_REL}` has an uncommitted change and it is only `enabledPlugins` "
        "entries — a plugin activation this repo is expected to carry (written by "
        "`spawn-workers`, or a manual `claude plugin enable -s project`). Commit it: "
        "fold it into this card's first commit, or make a standalone `chore` commit and "
        "push it skip-CI. Follow the repo's commit-convention."
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        session_dir = Path(
            os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
        )
        message = nudge(session_dir)
    except Exception:
        message = None

    out: dict = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
    if message:
        out["hookSpecificOutput"]["additionalContext"] = message
    print(json.dumps(out))


if __name__ == "__main__":
    main()
