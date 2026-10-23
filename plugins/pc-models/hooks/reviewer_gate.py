#!/usr/bin/env python3
"""PreToolUse (Agent) — deny the `reviewer` agent while `adversarial` has not been tried on a diff.

The review order (finishing-a-card.md) is: a change with a diff goes to the `adversarial` MCP tool
directly, and the `reviewer` agent only takes over when `adversarial` answered `BLOCKED` or `ERROR`
(or when there is no diff). This hook enforces it: an Agent call with subagent_type `reviewer`
(or `pc-core:reviewer`) is denied when the repo differs from its default branch (working tree and
untracked files included) and the
session transcript holds no `adversarial` tool_result opening with `[adversarial] BLOCKED` or
`[adversarial] ERROR`. Every other subagent_type, and a repo with no diff, passes.

Accepted limits: the hook fails open on any git or transcript error; one BLOCKED or ERROR anywhere
in the session transcript allows the reviewer for the rest of the session.

A transcript tool_result carries the tool's string either bare or wrapped as `{"result": "..."}`,
and `content` is a string or a list of `{"type": "text"}` blocks; all three are read.

Python 3.12+, stdlib only. A hook failure must never block a turn → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REVIEWER_TYPES = frozenset({"reviewer", "pc-core:reviewer"})
FAILURE_PREFIXES = ("[adversarial] BLOCKED", "[adversarial] ERROR")

DENY_REASON = (
    "The change has a diff against the default branch and `adversarial` has not failed this session: call the "
    "`adversarial` MCP tool directly (mcp__plugin_pc-models_pc-models__adversarial, with "
    '`base`, `tools: ["read", "grep"]` and the card\'s task). `reviewer` is launched only for a '
    "claim with no diff, or after `adversarial` answered BLOCKED or ERROR."
)


def _git(cwd: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git with a short timeout.

    Args:
        cwd: Directory to run git in; None means the hook's own cwd.
        *args: The git arguments.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd or None,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )


def resolve_base(cwd: str | None) -> str | None:
    """The repo's default branch: `origin/HEAD` if set, else the first of main/master that exists.

    Args:
        cwd: Directory to run git in.

    Returns:
        A ref name, or None when none can be resolved.
    """
    proc = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    for name in ("main", "master"):
        if (
            _git(
                cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"
            ).returncode
            == 0
        ):
            return name
    return None


def has_diff(cwd: str | None) -> bool:
    """Whether the work differs from the default branch, untracked files included.

    Args:
        cwd: Directory to run git in; None means the hook's own cwd.

    Returns:
        True when the tracked diff against the base is non-empty or an untracked file exists;
        False when the base is unresolvable or git fails.
    """
    try:
        base = resolve_base(cwd)
        if base is None:
            return False
        if _git(cwd, "diff", base, "--quiet", "--").returncode == 1:
            return True
        others = _git(cwd, "ls-files", "--others", "--exclude-standard")
    except (OSError, subprocess.SubprocessError):
        return False
    return others.returncode == 0 and bool(others.stdout.strip())


def result_text(content: object) -> str:
    """The plain text of a tool_result `content`, unwrapping a `{"result": ...}` envelope.

    Args:
        content: A string, or a list of text blocks.

    Returns:
        The text, empty when none.
    """
    if isinstance(content, list):
        content = "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(content, str):
        return ""
    try:
        wrapped = json.loads(content)
    except ValueError:
        return content
    if isinstance(wrapped, dict) and isinstance(wrapped.get("result"), str):
        return wrapped["result"]
    return content


def _line_failed(line: str) -> bool:
    """Whether one transcript line holds a BLOCKED or ERROR adversarial tool_result.

    Args:
        line: One JSONL transcript line.

    Returns:
        True when such a tool_result is present.
    """
    try:
        message = json.loads(line).get("message")
    except (ValueError, AttributeError):
        return False
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "tool_result"
        and result_text(block.get("content")).startswith(FAILURE_PREFIXES)
        for block in content
    )


def adversarial_failed(transcript_path: str | None) -> bool:
    """Whether the transcript holds an adversarial tool_result that is BLOCKED or ERROR.

    Args:
        transcript_path: The session JSONL transcript.

    Returns:
        True when one such tool_result exists; False when absent or unreadable.
    """
    if not transcript_path:
        return False
    try:
        with Path(transcript_path).open(encoding="utf-8") as transcript:
            for line in transcript:
                if "[adversarial]" in line and _line_failed(line):
                    return True
    except OSError:
        return False
    return False


def decide(payload: dict) -> dict | None:
    """The hook response for one Agent payload, or None to let it through.

    Args:
        payload: The PreToolUse hook input.

    Returns:
        A deny response, or None.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if tool_input.get("subagent_type") not in REVIEWER_TYPES:
        return None
    if not has_diff(payload.get("cwd")):
        return None
    if adversarial_failed(payload.get("transcript_path")):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }
    }


def main() -> int:
    """Read the payload from stdin and print the hook response, if any.

    Returns:
        The process exit code — always 0, so a hook failure never blocks a session.
    """
    try:
        response = decide(json.load(sys.stdin))
    except Exception:  # a guard must never block a turn on its own bug
        return 0
    if response is not None:
        sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())
