"""Tests for the pc-bash-guard PreToolUse hook."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "plugins/pc-bash-guard/hooks/dangerous_bash_ask.py"
)
_spec = importlib.util.spec_from_file_location("dangerous_bash_ask", _SCRIPT)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _payload(command: str, mode: str | None) -> dict[str, Any]:
    """Build a PreToolUse payload for a Bash command in the given permission mode."""
    payload: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}
    if mode is not None:
        payload["permission_mode"] = mode
    return payload


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.pyc' -delete",
        r"find . -type f -exec rm {} \;",
        "rtk find . -name x -execdir rm {} +",
        r"cd x && find . -ok rm {} \;",
        "echo hi > out.txt",
        "echo hi >> out.txt",
        "echo hi>out.txt",
    ],
)
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", None])
def test_dangerous_form_asks_outside_auto(command: str, mode: str | None) -> None:
    """A destructive form asks in every mode but auto."""
    response = hook.decide(_payload(command, mode))
    assert response is not None
    assert response["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.pyc' -delete",
        "echo hi > out.txt",
    ],
)
def test_auto_mode_never_asks(command: str) -> None:
    """In auto mode the classifier decides — the hook stays silent."""
    assert hook.decide(_payload(command, "auto")) is None


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.py'",
        "find . -type f -newer x",
        "rtk find . -name x",
        "echo hello",
        "echo hi 2>&1",
        "echo hi | grep h",
        "git log --format=%H > /dev/null",
        "ls -la",
    ],
)
def test_safe_forms_pass(command: str) -> None:
    """Read-only uses of find and echo pass untouched."""
    assert hook.decide(_payload(command, "acceptEdits")) is None


def test_reason_names_the_mode() -> None:
    """The prompt reason shows the mode the hook saw."""
    response = hook.decide(_payload("echo a > b", "acceptEdits"))
    assert response is not None
    assert "acceptEdits" in response["hookSpecificOutput"]["permissionDecisionReason"]


def test_malformed_payload_passes() -> None:
    """A payload without a command string never blocks."""
    assert hook.decide({"tool_input": {}}) is None
    assert hook.decide({"tool_input": "x"}) is None


def test_response_oracle() -> None:
    """The whole hook response is pinned, key by key."""
    assert hook.decide(_payload("echo a > b", "acceptEdits")) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "pc-bash-guard: echo redirected to a file (permission mode: acceptEdits)",
        }
    }
