#!/usr/bin/env python3
"""SessionStart hook — start every session on a clean, up-to-date base.

Calls the deterministic logic in `pc-git:clean-gone` skill's `bulk_clean_gone.sh`
script as a subprocess with --quiet mode, on the session's own repo only: fast-forwards
the base branch, deletes local branches marked [gone] (remote deleted post-merge or
otherwise) along with their worktrees, and moves HEAD to the base only when HEAD itself
was [gone].

`SessionStart` rather than `SessionEnd`: a PR merges after the session that opened it has
ended, so an end-of-session sweep always runs one merge too early. The sweep runs
synchronously — a background checkout would switch branches under a session that has
already started reading files.

A root that is not itself a git repo (a session opened at the workspace root) is skipped:
sweeping every repo beneath it would block the session start for tens of seconds, for a
session that works in none of them.

Whatever the script did is surfaced as a `systemMessage` for the user and as
`additionalContext` for the model; nothing done means no output at all.

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

SCRIPT_TIMEOUT = 25


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


def build_hook_output(report: str) -> dict:
    """The SessionStart JSON envelope carrying the script's report to user and model."""
    message = f"clean-gone:\n{report}"
    return {
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        },
    }


def run_hook() -> int:
    """Read the SessionStart event from stdin, call bulk_clean_gone.sh, never raise."""
    try:
        event = read_event()
        root = project_root(event)

        # `.git` fichier ou dossier : un worktree compte comme un repo
        if not (root / ".git").exists():
            return 0

        script_path = (
            Path(__file__).parent.parent.parent
            / "skills"
            / "clean-gone"
            / "scripts"
            / "bulk_clean_gone.sh"
        )
        if not script_path.is_file():
            return 0

        result = subprocess.run(
            ["bash", str(script_path), "--root", str(root), "--quiet"],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            check=False,
        )
        report = result.stdout.rstrip()
        if report:
            print(json.dumps(build_hook_output(report), ensure_ascii=False))
    except Exception:
        # Un hook ne doit jamais échouer — toute erreur inattendue sort silencieusement
        pass

    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
