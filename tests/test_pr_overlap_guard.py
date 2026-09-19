"""Tests for pr_overlap_guard hook."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GUARD_PATH = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-git"
    / "hooks"
    / "pr-overlap"
    / "pr_overlap_guard.py"
)
sys.path.insert(0, str(GUARD_PATH.parent.parent.parent))  # noqa: E402


def run_hook(event: dict) -> dict | None:
    """Run the hook script and return parsed decision, or None if no decision."""
    try:
        result = subprocess.run(
            [sys.executable, str(GUARD_PATH)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def test_non_bash_tool_passes():
    """Non-Bash tools should pass (allow)."""
    event = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "README.md"},
    }
    decision = run_hook(event)
    assert decision is None


def test_non_guarded_bash_command_passes():
    """Bash commands outside guard scope should pass."""
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    }
    decision = run_hook(event)
    assert decision is None


def test_missing_stdin_passes():
    """Missing stdin should pass gracefully."""
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input="",
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert not result.stdout.strip()


def test_malformed_json_passes():
    """Malformed JSON in stdin should pass gracefully."""
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input="not valid json",
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert not result.stdout.strip()


def test_guard_detects_git_switch_c_command():
    """Guard regex recognizes git switch -c."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pr_overlap_guard", GUARD_PATH)
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)

    assert guard_module.is_guarded_command("git switch -c feature/foo")
    assert guard_module.is_guarded_command("git switch -c fix/bar")


def test_guard_detects_git_checkout_b_command():
    """Guard regex recognizes git checkout -b."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pr_overlap_guard", GUARD_PATH)
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)

    assert guard_module.is_guarded_command("git checkout -b fix/bar")
    assert guard_module.is_guarded_command("git checkout -b feature/test")


def test_guard_detects_gh_pr_create_command():
    """Guard regex recognizes gh pr create."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pr_overlap_guard", GUARD_PATH)
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)

    assert guard_module.is_guarded_command("gh pr create --title 'My PR'")
    assert guard_module.is_guarded_command("gh pr create")


def test_guard_detects_glab_mr_create_command():
    """Guard regex recognizes glab mr create."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pr_overlap_guard", GUARD_PATH)
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)

    assert guard_module.is_guarded_command("glab mr create")
    assert guard_module.is_guarded_command("glab mr create --description 'test'")


def test_excluded_files_do_not_trigger_overlap():
    """Overlap on only .lock/.toml files should be allowed."""
    # Load the guard module dynamically to test the filter function.
    import importlib.util

    spec = importlib.util.spec_from_file_location("pr_overlap_guard", GUARD_PATH)
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)

    files = {"pyproject.toml", "uv.lock", "src/main.py"}
    filtered = guard_module._filter_excluded(files)
    assert filtered == {"src/main.py"}
    assert "pyproject.toml" not in filtered
    assert "uv.lock" not in filtered


def test_decision_structure():
    """Decision structure matches branch_guard.py format."""
    # Load the guard module dynamically to test the _decision function.
    import importlib.util

    spec = importlib.util.spec_from_file_location("pr_overlap_guard", GUARD_PATH)
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)

    decision = guard_module._decision("deny", "test reason")
    assert "hookSpecificOutput" in decision
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert decision["hookSpecificOutput"]["permissionDecisionReason"] == "test reason"
    assert decision["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_guard_behavior_on_missing_tool():
    """When gh/glab missing, guard should pass (allow), never block on tool error."""
    # This is implicit in the hook's error handling: any exception in decide()
    # returns 0 without printing a decision. We verify indirectly by ensuring
    # the script exits 0 even if gh/glab are not installed.
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "git switch -c feature/test"},
        "cwd": "/tmp",  # a path unlikely to be a real repo
    }
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=5,
    )
    # Even if the repo or tools don't exist, the script should exit 0 (not raise)
    assert result.returncode == 0
