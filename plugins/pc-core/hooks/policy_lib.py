#!/usr/bin/env python3
"""policy_lib — shared resolution for `.claude/policies.json` in a multi-repo workspace.

Never declared in `hooks.json` and never invoked by a skill's DCI directly — it is a pure
dependency, imported by three callers that each have their own entry point:
`inject_policies.py` (PreToolUse hook), `branch_guard.py` (PreToolUse hook), and
`resolve_policy.py` (DCI CLI, called from a skill body). Splitting a resolution path three files
deep once was already a maintenance smell (`resolve_policy.py`'s docstring used to point at
`inject_policies.py` for it) — this file exists so that dependency shows up as an import
statement (`from policy_lib import ...`) instead of one hook reaching into another's internals.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def resolve_repo_root(project_dir: str, file_path: str | None) -> Path:
    """Resolve the git repo root for `file_path` (or `project_dir` if none), via `git rev-parse`."""
    target = Path(file_path).parent if file_path else Path(project_dir)
    try:
        out = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0:
            return Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path(project_dir)


def load_policies(repo_root: Path) -> dict | None:
    """Load `.claude/policies.json` from `repo_root`, or None if absent/unreadable."""
    policies_path = repo_root / ".claude" / "policies.json"
    if not policies_path.is_file():
        return None
    try:
        return json.loads(policies_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
