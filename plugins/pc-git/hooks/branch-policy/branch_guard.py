#!/usr/bin/env python3
"""PreToolUse — hard-block a guarded write on a permanent branch (replaces `branch.md`).

The `branch-workflow` skill already documents this guard as a prompt-followed sequence — "any
skill about to `git add`/`git commit` applies this before staging". That is no enforcement:
GitHub branch protection ("require PR before merge") is a paid feature on a private org repo, so
a direct push to `main` is never server-side rejected. This hook is the actual gate.

Fires on `Edit`/`Write`/`MultiEdit`/`NotebookEdit`. Reads the repo's declared `branch_workflow`
from `.claude/settings.pinky.json` (via `hooks/policy_lib.py`'s `load_policies`/`resolve_repo_root`
— same resolution `resolve_policy.py` uses for the DCI path) and the current git branch. Denies
only when BOTH hold: the current branch is one of that strategy's permanent branches, AND the
target path is not in the non-guarded list (`branch_paths.sh`, this folder — shared with
`bulk_commit.sh`). Undeclared strategy, non-git target, or an already-checked-out topic branch
all degrade to "nothing to enforce" — same graceful-absence philosophy as `resolve_policy.py`.

Lives in `hooks/branch-policy/`, a concern folder alongside the other 3 files that implement the
same concern (`bulk_commit.sh`, `resolve_policy.py`, `branch_paths.sh`) — `hooks/` itself stays
the one plugin-root directory the harness recognises for everything hook-related; `policy_lib.py`
sits one level up, in `hooks/` directly, genuinely shared with `inject_policies.py` outside this
concern.

Python 3.12+, stdlib only. A hook failure must never block a tool call → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pc_git_policy_lib import load_policies, resolve_repo_root  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

GUARDED_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
BRANCH_PATHS_SH = Path(__file__).parent / "branch_paths.sh"

# Permanent branches per strategy — the `branch-workflow` skill's own table. GitLab Flow's
# environment branches (staging, production, ...) and Release Branching's `develop` pairing
# aren't statically named anywhere; `main` alone is the safe minimum enforced for those two.
_PERMANENT_BRANCHES: dict[str, set[str]] = {
    "feature branching": {"main"},
    "git flow": {"main", "develop"},
    "github flow": {"main"},
    "gitlab flow": {"main"},
    "release branching": {"main"},
    "trunk-based development": {"main", "trunk"},
}


def project_root(event: dict | None = None) -> Path:
    """Resolve the project root: $CLAUDE_PROJECT_DIR > event cwd > current dir."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        return Path(proj)
    if event and event.get("cwd"):
        return Path(event["cwd"])
    return Path.cwd()


def target_path(tool_name: str, tool_input: dict) -> str | None:
    """Extract the file a write-class tool targets (NotebookEdit uses `notebook_path`)."""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path")
    return tool_input.get("file_path")


def current_branch(repo_root: Path) -> str | None:
    """The repo's current branch, `""` on detached HEAD, or `None` when not a usable git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def relative_target(target: str, repo_root: Path) -> str:
    """`target` expressed relative to `repo_root`, matching `git diff --name-only`'s shape."""
    tgt = Path(target)
    tgt = (tgt if tgt.is_absolute() else repo_root / tgt).resolve()
    try:
        return str(tgt.relative_to(repo_root.resolve()))
    except ValueError:
        return target  # outside the repo entirely — won't match any glob, stays guarded


def is_unguarded(rel_path: str) -> bool:
    """Delegate to `branch_paths.sh` — the one glob list shared with `bulk_commit.sh`, this folder."""
    result = subprocess.run(
        ["bash", str(BRANCH_PATHS_SH), rel_path], timeout=2, check=False
    )
    return result.returncode == 0


def _decision(kind: str, reason: str) -> dict:
    """Wrap a PreToolUse permission decision in the harness-expected envelope."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }


def decide(event: dict, root: Path) -> dict | None:
    """Core hook decision: deny a guarded write on a permanent branch, else None (let it proceed)."""
    tool = event.get("tool_name", "")
    if tool not in GUARDED_TOOLS:
        return None
    target = target_path(tool, event.get("tool_input") or {})
    if not target:
        return None

    repo_root = resolve_repo_root(str(root), target)
    branch = current_branch(repo_root)
    if not branch:
        return None  # not a usable git repo, or detached HEAD — nothing to enforce

    strategy = (
        str((load_policies(repo_root) or {}).get("branch_workflow", "")).strip().lower()
    )
    permanent = _PERMANENT_BRANCHES.get(strategy)
    if not permanent or branch not in permanent:
        return None  # undeclared strategy, or already on a topic branch

    rel = relative_target(target, repo_root)
    if is_unguarded(rel):
        return None

    return _decision(
        "deny",
        f"branch guard ({strategy}): `{rel}` is not on the non-guarded list, and `{branch}` is a "
        "permanent branch for this repo's strategy. Create a feature branch first "
        "(`git checkout -b feature/<name>`), then retry.",
    )


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        decision = decide(event, project_root(event))
    except Exception:  # a guard must never block a tool call on its own bug
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
