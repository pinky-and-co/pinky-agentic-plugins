#!/usr/bin/env python3
"""PreToolUse — guard against overlapping PRs (same files in flight).

When a session opens a branch or PR for a change, there may already be PRs or
local branches touching the same files. This hook detects recouvrement (overlap)
and refuses, naming the PR/branch in conflict. It fires on:
  - `git switch -c <branch>`
  - `git checkout -b <branch>`
  - `gh pr create`
  - `glab mr create`

Excluded files (lockfiles: uv.lock, pyproject.toml, package-lock.json, poetry.lock)
are filtered — two branches touching only these files do not trigger overlap.
This is a universal property of package managers, hardcoded and not configurable.

A false positive (blocking a legitimate change) is worse than a false negative
(missing an overlap). When in doubt, let it proceed. Never block on a tool error
(gh/glab missing or timing out, no remote repo, detached HEAD, etc.) — exit 0.

Python 3.12+, stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pc_git_policy_lib import resolve_repo_root  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

EXCLUDED_FILES = {"uv.lock", "pyproject.toml", "package-lock.json", "poetry.lock"}


def project_root(event: dict | None = None) -> Path:
    """Resolve project root: $CLAUDE_PROJECT_DIR > event cwd > current dir."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        return Path(proj)
    if event and event.get("cwd"):
        return Path(event["cwd"])
    return Path.cwd()


def _decision(kind: str, reason: str) -> dict:
    """Wrap a PreToolUse permission decision in the harness-expected envelope."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }


def extract_command(tool_input: dict) -> str:
    """Extract the shell command from a Bash tool_input."""
    return tool_input.get("command", "")


def is_guarded_command(command: str) -> bool:
    """Check if command is one we guard: git switch -c, git checkout -b, gh/glab pr/mr create."""
    guards = [
        r"\bgit\s+switch\s+-c\b",
        r"\bgit\s+checkout\s+-b\b",
        r"\b(gh\s+pr|glab\s+mr)\s+create\b",
    ]
    return any(re.search(pattern, command) for pattern in guards)


def current_branch(repo_root: Path) -> str | None:
    """Get current branch name, None if not in git or detached HEAD."""
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


def _resolve_base_branch(repo_root: Path) -> str | None:
    """Resolve the integration branch, or None when no candidate exists.

    Mimics resolve_base() in branch-policy/bulk_commit.sh: the remote's default branch via
    symbolic ref first, then a local `main`/`master`. None means the caller must abstain —
    never HEAD, which would make a `base...HEAD` comparison empty by construction.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip().removeprefix("origin/")
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Try main or master (common integration branch names)
    for candidate in ("main", "master"):
        try:
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{candidate}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if out.returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            pass

    return None


def _diff_files(repo_root: Path, base: str = "HEAD") -> set[str]:
    """Files changed in current working tree vs base (default HEAD)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "diff", base, "--name-only"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    return set(f for f in out.stdout.strip().split("\n") if f)


def _open_pr_files_github(repo_root: Path) -> dict[str, set[str]]:
    """Fetch files from open PRs on GitHub via gh pr list."""
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,headRefName,files",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    try:
        prs = json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        return {}

    result = {}
    for pr in prs:
        pr_num = pr.get("number")
        branch = pr.get("headRefName", "")
        files = pr.get("files", [])
        if pr_num and branch:
            result[f"PR #{pr_num} ({branch})"] = set(
                f.get("name", "") for f in files if f.get("name")
            )
    return result


def _open_pr_files_gitlab(repo_root: Path) -> dict[str, set[str]]:
    """Fetch files from open MRs on GitLab via glab mr list."""
    try:
        out = subprocess.run(
            [
                "glab",
                "mr",
                "list",
                "--state",
                "opened",
                "--json",
                "iid,sourceBranch,changedFiles",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    try:
        mrs = json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        return {}

    result = {}
    for mr in mrs:
        mr_id = mr.get("iid")
        branch = mr.get("sourceBranch", "")
        files = mr.get("changedFiles", [])
        if mr_id and branch:
            result[f"MR !{mr_id} ({branch})"] = (
                set(files) if isinstance(files, list) else set()
            )
    return result


def _local_branch_files(repo_root: Path) -> dict[str, set[str]]:
    """Files in local branches (vs their tracking branch or resolved base branch)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "branch"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}

    result = {}
    for line in out.stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        branch = line.lstrip("* ").strip()
        try:
            upstream = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "rev-parse",
                    "--abbrev-ref",
                    f"{branch}@{{upstream}}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            base = (
                _resolve_base_branch(repo_root)
                if upstream.returncode != 0
                else upstream.stdout.strip()
            )
            if base is None:
                continue
            diff_out = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "diff",
                    f"{base}..{branch}",
                    "--name-only",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if diff_out.returncode == 0:
                files = set(f for f in diff_out.stdout.strip().split("\n") if f)
                if files:
                    result[f"branch {branch}"] = files
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def _filter_excluded(files: set[str]) -> set[str]:
    """Remove excluded files (lockfiles) from a set."""
    return {f for f in files if f not in EXCLUDED_FILES}


def _find_overlap(
    current_diff: set[str],
    in_flight: dict[str, set[str]],
) -> tuple[str, set[str]] | None:
    """Check overlap between current diff and in-flight PRs/branches.

    Return (source_name, overlapping_files) if non-empty overlap found, else None.
    """
    current_filtered = _filter_excluded(current_diff)
    for source, files in in_flight.items():
        files_filtered = _filter_excluded(files)
        overlap = current_filtered & files_filtered
        if overlap:
            return (source, overlap)
    return None


def decide(event: dict, root: Path) -> dict | None:
    """Core hook decision: deny if overlap detected with in-flight work, else None."""
    if event.get("tool_name") != "Bash":
        return None

    command = extract_command(event.get("tool_input") or {})
    if not is_guarded_command(command):
        return None

    try:
        repo_root = resolve_repo_root(str(root), None)
    except (OSError, subprocess.CalledProcessError):
        return None

    is_pr_create = bool(re.search(r"\b(gh\s+pr|glab\s+mr)\s+create\b", command))
    if is_pr_create:
        base = _resolve_base_branch(repo_root)
        if base is None:
            return None
        current_diff = _diff_files(repo_root, f"{base}...HEAD")
    else:
        current_diff = _diff_files(repo_root, "HEAD")

    if not current_diff:
        return None

    in_flight = {}
    in_flight.update(_open_pr_files_github(repo_root))
    in_flight.update(_open_pr_files_gitlab(repo_root))
    in_flight.update(_local_branch_files(repo_root))

    if not in_flight:
        return None

    overlap_result = _find_overlap(current_diff, in_flight)

    if overlap_result:
        source, files = overlap_result
        files_str = ", ".join(sorted(files))
        return _decision(
            "deny",
            f"pr-overlap guard: {files_str} already in flight ({source}). "
            f"Push to that PR/branch instead, or wait for it to merge.",
        )

    return None


def run_hook() -> int:
    """Read PreToolUse event from stdin, emit decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        decision = decide(event, project_root(event))
    except Exception:
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
