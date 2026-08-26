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
    """Merge `.claude/policies.json` from `repo_root` up through every parent directory to the
    filesystem root — the same walk-up Claude Code itself already does for `CLAUDE.md`. Closest
    wins per key: a key `repo_root` declares is never overridden by an ancestor; a key `repo_root`
    omits falls back to the nearest ancestor that declares it. No key is exempt — a per-key
    special case is complexity nothing has asked for. Returns None only if no `.claude/
    policies.json` exists anywhere in the chain (never an empty dict for "found but empty").
    """
    merged: dict = {}
    found = False
    current = repo_root
    while True:
        policies_path = current / ".claude" / "policies.json"
        if policies_path.is_file():
            try:
                data = json.loads(policies_path.read_text())
            except (json.JSONDecodeError, OSError):
                data = None  # malformed at this level — skip it, keep walking up
            if data is not None:
                for key, value in data.items():
                    merged.setdefault(key, value)
                found = True
        if current.parent == current:
            break
        current = current.parent
    return merged if found else None
