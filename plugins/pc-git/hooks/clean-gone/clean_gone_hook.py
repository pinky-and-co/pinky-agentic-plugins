#!/usr/bin/env python3
"""SessionEnd hook — automatically clean [gone] branches when a session ends.

Calls the deterministic logic in `pc-git:clean-gone` skill's `bulk_clean_gone.sh`
script as a subprocess with --quiet mode for silent operation.

Fires on `SessionEnd` (when a session properly terminates, user stops interaction).
Never blocks the session — any error exits gracefully with code 0. Sweeps the
workspace root (via CLAUDE_PROJECT_DIR) for local branches marked [gone] (remote
deleted post-merge or otherwise) and removes them along with their worktrees.

A `SessionEnd` hook is preferred over `SessionStart` because it cleans what the
current session left behind, ready for the next session to start clean. `Stop`
hooks (end of assistant turn) are avoided to prevent wasteful sweeps at every turn.

Python 3.12+, stdlib only. A hook failure must never block a session → unexpected
errors exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def read_event() -> dict:
    """Parse the hook JSON from stdin; tolerate an empty / malformed payload."""
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}


def project_root(event: dict | None = None) -> Path:
    """Resolve the project root: $CLAUDE_PROJECT_DIR > event cwd > current dir."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        return Path(proj)
    if event and event.get("cwd"):
        return Path(event["cwd"])
    return Path.cwd()


def run_hook() -> int:
    """Read the SessionEnd event from stdin, call bulk_clean_gone.sh, never raise."""
    try:
        event = read_event()
        root = project_root(event)

        # Si le root n'existe pas ou n'est pas un répertoire, skip silencieusement
        if not root.is_dir():
            return 0

        # Appelle le script de nettoyage du skill clean-gone
        script_path = (
            Path(__file__).parent.parent.parent
            / "skills"
            / "clean-gone"
            / "scripts"
            / "bulk_clean_gone.sh"
        )
        if not script_path.is_file():
            return 0

        # Exécute le script en mode silencieux
        subprocess.run(
            ["bash", str(script_path), "--root", str(root), "--quiet"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except Exception:
        # Un hook ne doit jamais échouer — toute erreur inattendue sort silencieusement
        pass

    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
