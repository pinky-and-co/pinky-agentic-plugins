#!/usr/bin/env python3
"""SessionStart hook — keep the workspace and group AGENTS.md files in sync with the disk.

Resolves the workspace (`PINKY_WORKSPACE_ROOT`, else the nearest `*.code-workspace` above the
session's directory; never a repo nor `$HOME`), renders the workspace AGENTS.md and every group's,
and rewrites a file only when its content changed. One JSON line names the rewritten files;
nothing rewritten means no output at all. No workspace resolved means a silent no-op. A symlinked
AGENTS.md is never touched. A hand-written `##` section in a rewritten file is replaced (only the
title and intro survive), and the message says so.

Stays importable and runnable on Python 3.9: the library import happens inside the guard, so a
failing import exits 0 silently. An interpreter without `tomllib` (before 3.11) does nothing: it
could only write blank descriptions and flip-flop the files against a newer one. A hook failure must never block a session, so every path exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

LOSSY_NOTE = "sections outside the generated blocks are replaced"


@dataclass
class SyncResult:
    """Files rewritten by one sync, relative to the workspace root."""

    written: list[str] = field(default_factory=list)
    lossy: list[str] = field(default_factory=list)


def read_event() -> Mapping[str, object]:
    """Parse the hook JSON from stdin; tolerate an empty / malformed payload."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def project_root(event: Mapping[str, object]) -> Path:
    """Resolve the start directory: $CLAUDE_PROJECT_DIR > event cwd > current dir."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        return Path(proj)
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path.cwd()


def build_hook_output(message: str) -> Mapping[str, object]:
    """The SessionStart JSON envelope carrying the message to user and model."""
    return {
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        },
    }


def write_if_changed(path: Path, content: str) -> bool:
    """Atomically write `content` to `path` unless it already holds exactly that.

    A symlinked target is skipped untouched.

    Args:
        path: Target file.
        content: Wanted content.

    Returns:
        True when the file was created or rewritten.
    """
    from agents_md_lib import atomic_write

    if path.is_symlink():
        return False
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    atomic_write(path, content)
    return True


def sync_workspace(workspace_root: Path) -> SyncResult:
    """Regenerate the workspace and group AGENTS.md files.

    Args:
        workspace_root: The workspace directory.

    Returns:
        The files created or rewritten, and those that lost a hand-written section.
    """
    from agents_md_lib import (
        GROUP_HEADINGS,
        WORKSPACE_HEADINGS,
        discover_groups,
        has_foreign_sections,
        render_group_agents_md,
        render_workspace_agents_md,
    )

    result = SyncResult()
    try:
        groups = discover_groups(workspace_root)
    except OSError:
        groups = []
    for target in [workspace_root, *groups]:
        path = target / "AGENTS.md"
        try:
            if path.is_symlink():
                continue
            existing = path.read_text(encoding="utf-8") if path.is_file() else None
            if target == workspace_root:
                content = render_workspace_agents_md(workspace_root, existing)
                allowed = WORKSPACE_HEADINGS
            else:
                content = render_group_agents_md(target, workspace_root, existing)
                allowed = GROUP_HEADINGS
            if write_if_changed(path, content):
                relative = str(path.relative_to(workspace_root))
                result.written.append(relative)
                if has_foreign_sections(existing, allowed):
                    result.lossy.append(relative)
        except Exception:
            # Un fichier en échec ne doit pas empêcher les autres
            continue
    return result


def run_hook() -> int:
    """Read the SessionStart event, sync the AGENTS.md files, never raise."""
    try:
        from agents_md_lib import find_workspace_root, tomllib_available

        # Sans tomllib les descriptions seraient vidées : deux interpréteurs feraient osciller les
        # fichiers, donc on ne touche à rien
        if not tomllib_available():
            return 0
        event = read_event()
        root = find_workspace_root(project_root(event), os.environ)
        if root is None:
            return 0
        result = sync_workspace(root)
        if result.written:
            message = f"agents-md: rewrote {', '.join(result.written)}"
            if result.lossy:
                message += f"; {LOSSY_NOTE} in {', '.join(result.lossy)}"
            print(json.dumps(build_hook_output(message), ensure_ascii=False))
    except Exception:
        # Un hook ne doit jamais échouer — toute erreur inattendue sort silencieusement
        pass
    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
