#!/usr/bin/env python3
"""PostToolUse hook — run the checker(s) relevant to the file just written, same turn.

The checkers this hook calls are resolved relative to the EDITED FILE'S OWN repo root
(`<repo>/scripts/hooks/*.py`), never relative to this hook's own location — a repo's hygiene
checkers are typically distributed there by that repo's own tooling (pre-commit, CI), independent
of whichever harness or plugin renders this hook. They already run at commit time via
`.pre-commit-config.yaml` — this hook adds no new detection, it moves the *feedback loop* earlier:
without it, several edits happen, then the checkers fire together at commit time, far from each
file's own context. Routing here mirrors `.pre-commit-config.yaml`'s own `files:`/`types:` filters
(check-zws, check-latex-notation and check-forbidden-keywords are universal; check-typed-api is `src/`-only;
check-weak-assertions is `tests/`-only) so a nudge here never disagrees with what CI actually
enforces. A checker not present in a given repo's `scripts/hooks/` is silently skipped — not every
repo carries every checker.

Contract: `exit 2` + stderr surfaces the message to Claude in the SAME turn, before its next tool
call — the write already happened and can't be un-done, so this can only inform, never block (the
blocking half of this pair is a PreToolUse guard). A bug in here must never break a turn, so the
whole dispatch is wrapped and defaults to silent exit 0.

Output is bounded — MAX_OUTPUT_LINES caps what reaches stderr, regardless of how many checkers
fired or how verbose their own output is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MAX_OUTPUT_LINES = 20
_CHECKERS_SUBDIR = ("scripts", "hooks")

_UNIVERSAL_CHECKERS = (
    "check_zws.py",
    "check_latex_notation.py",
    "check_forbidden_keywords.py",
)


def _is_disabled() -> bool:
    disabled = os.environ.get("PINKY_DISABLED_HOOKS", "")
    return Path(__file__).stem in {h.strip() for h in disabled.split(",") if h.strip()}


def target_path(tool_name: str, tool_input: dict) -> str | None:
    """Extract the file a write-class tool targets (NotebookEdit uses `notebook_path`)."""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path")
    return tool_input.get("file_path")


def _repo_root_and_relative(path_str: str) -> tuple[Path, str] | None:
    """The nearest ``.git`` ancestor plus the POSIX-style path relative to it, or None outside any
    repo (a kanban card, say) — no git, no scope."""
    try:
        path = Path(path_str).resolve()
    except OSError:
        return None
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            try:
                return parent, path.relative_to(parent).as_posix()
            except ValueError:
                return None
    return None


def _applicable_checkers(rel_path: str) -> list[str]:
    checkers = list(_UNIVERSAL_CHECKERS)
    if rel_path.endswith(".py"):
        if rel_path.startswith("src/"):
            checkers.append("check_typed_api.py")
        if rel_path.startswith("tests/"):
            checkers.append("check_weak_assertions.py")
    return checkers


def _run_checker(script: Path, filepath: str) -> str | None:
    """The checker's stdout report if it found a violation, else None. Never raises."""
    if not script.is_file():
        return None
    try:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        result = subprocess.run(
            [sys.executable, str(script), filepath],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            env=env,
        )
    except Exception:
        return None
    if result.returncode == 0:
        return None
    # Without an explicit encoding, a checker's UTF-8 ✓/❌ output can crash the pipe's reader
    # thread on a platform whose default codepage can't decode it, leaving `result.stdout` `None`
    # — the `or None` below covers that residual case defensively.
    return (result.stdout or "").strip() or None


def decide(event: dict) -> str | None:
    """The bounded stderr report for *event*, or None if nothing to say (clean file, no target,
    outside any repo, or the file's extension/path has no applicable checker)."""
    filepath = target_path(event.get("tool_name", ""), event.get("tool_input") or {})
    if not filepath:
        return None
    located = _repo_root_and_relative(filepath)
    if located is None:
        return None
    root, rel = located
    checkers_dir = root.joinpath(*_CHECKERS_SUBDIR)

    reports = [
        out
        for name in _applicable_checkers(rel)
        if (out := _run_checker(checkers_dir / name, filepath)) is not None
    ]
    if not reports:
        return None

    lines = "\n".join(reports).splitlines()
    bounded = lines[:MAX_OUTPUT_LINES]
    if len(lines) > MAX_OUTPUT_LINES:
        bounded.append(f"   … {len(lines) - MAX_OUTPUT_LINES} more line(s), truncated")
    return "\n".join(bounded)


def main() -> int:
    if _is_disabled():
        return 0
    event = json.loads(sys.stdin.read() or "{}")
    report = decide(event)
    if report is None:
        return 0
    print(report, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A hook failure must never block a turn.
        sys.exit(0)
