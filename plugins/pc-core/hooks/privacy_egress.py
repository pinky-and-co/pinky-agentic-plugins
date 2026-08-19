#!/usr/bin/env python3
"""PreToolUse hook — block sensitive-data egress to the cloud model.

Defines "done": on a ``Read`` — or a read-like ``Bash`` command (cat/head/tail/…, a channel a
permissive baseline often pre-allows prompt-free) — whose target file CONTENT carries
secret-shaped data, return ``permissionDecision: "deny"`` — that is the boundary where the content
has not yet entered the context (hence not yet been sent to the cloud model). Other tools and
clean files yield no decision (the flow proceeds). The hook never raises: a guard must not block a
tool call on its own bug. Honest scope: it stops the ACCIDENT, not determined extraction —
grep/awk/interpreters are open channels by design; the real barriers stay a repo's own pre-commit
gates and its `permissions.deny` baseline.

Detection reuses `secret_patterns.py` — the one "secret shapes" module, shared with whatever else
needs the same detector (minimal-code: no second pattern list). Lines opting out with
``# pragma: allowlist secret`` (or ``gitleaks:allow``) are honoured exactly like the pre-commit
secret hooks, so a deliberately-allowlisted fixture is not blocked.

Path-level protection (``.env``, ``~/.ssh``, ``~/.aws``) belongs to a repo's own
``permissions.deny`` baseline; this hook is the *content* level — real creds inside an otherwise
allowed file.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from secret_patterns import secret_kinds  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

MAX_SCAN_BYTES = (
    256 * 1024
)  # scan the head of large files; never load a multi-GB dump into the hook
_ALLOWLIST_MARKERS = ("allowlist secret", "gitleaks:allow")
MAX_REPORTED_LINES = (
    10  # bound the line list in the deny reason (hook output stays bounded)
)

# Bash commands that dump a file's CONTENT into the context — the same egress as a Read. Covering
# them closes the frictionless bypass; grep/awk/interpreters stay out of scope (this guard stops
# the accident, not determined extraction).
_READ_CMDS = {"cat", "head", "tail", "less", "more", "strings"}
MAX_BASH_TARGETS = 8  # bound the per-call scan cost


def _decision(kind: str, reason: str) -> dict:
    """Wrap a PreToolUse permission decision in the harness-expected envelope."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }


def is_git_tracked(path: Path) -> bool:
    """True if `path` is tracked by git. Tracking alone is NOT vetting — see `is_vetted`."""
    try:
        r = subprocess.run(
            [
                "git",
                "-C",
                str(path.parent),
                "ls-files",
                "--error-unmatch",
                "--",
                path.name,
            ],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def is_vetted(path: Path) -> bool:
    """True when `path` is git-tracked AND identical to HEAD — content that actually cleared a
    pre-commit secret gate. Re-scanning it would be redundant. A staged or modified tracked file
    has passed NO gate (pre-commit runs at commit time), so it is scanned exactly like an
    untracked one. Outside a git repo (or on git failure) → not vetted → scanned."""
    if not is_git_tracked(path):
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(path.parent), "status", "--porcelain", "--", path.name],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and not r.stdout.strip()


def secret_lines(path: Path) -> list[tuple[int, str]]:
    """(1-based line number, reason) for each (head) line carrying secret-shaped data, skipping
    allowlisted lines. Empty = clean."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:MAX_SCAN_BYTES]
    except (OSError, ValueError):
        return []  # unreadable / binary → not our call; let the Read itself deal with it
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(marker in line for marker in _ALLOWLIST_MARKERS):
            continue
        kinds = secret_kinds(line)
        if kinds:
            hits.append((lineno, " + ".join(kinds)))
    return hits


def bash_read_targets(command: str) -> list[Path]:
    """Existing files a simple read-like Bash segment (cat/head/tail/less/more/strings) would dump
    into the context. Conservative token scan, NOT a shell parser."""
    targets: list[Path] = []
    for segment in re.split(r"[|;&]+", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:  # unbalanced quotes etc. — best effort
            tokens = segment.split()
        if not tokens or Path(tokens[0]).name not in _READ_CMDS:
            continue
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            p = Path(tok).expanduser()
            if p.is_file():
                targets.append(p)
    return targets[:MAX_BASH_TARGETS]


def decide(event: dict) -> dict | None:
    """Deny a Read — or a read-like Bash command — whose target file contains sensitive data.

    Same boundary for both channels: the content has not yet entered the context. Otherwise None."""
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    if tool == "Read":
        file_path = tool_input.get("file_path")
        targets = [Path(file_path)] if file_path else []
    elif tool == "Bash":
        targets = bash_read_targets(tool_input.get("command", ""))
    else:
        return None
    for path in targets:
        if is_vetted(path):
            continue  # cleared the commit gate — never re-block the repo's own files
        hits = secret_lines(path)
        if hits:
            shown = "; ".join(
                f"line {n} ({why})" for n, why in hits[:MAX_REPORTED_LINES]
            )
            if len(hits) > MAX_REPORTED_LINES:
                shown += f"; … (+{len(hits) - MAX_REPORTED_LINES} more)"
            return _decision(
                "deny",
                f"Privacy guard: `{path}` contains secret-shaped data — {shown} — reading it "
                f"would send it to the cloud model. Run the task locally instead, or mark a false "
                f"positive with `# pragma: allowlist secret` on the offending line.",
            )
    return None


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        decision = decide(event)
    except Exception:  # a guard must never block a tool call on its own bug
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
