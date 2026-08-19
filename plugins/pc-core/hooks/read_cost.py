#!/usr/bin/env python3
"""PreToolUse — warn before a native `Read` of an expensive format.

The problem isn't knowing how to read these formats — it's that reading them natively is
expensive. A `.pdf` or `.html` reads without error and lands whole in context, markup and all.
That is precisely what a "readable / unreadable" check misses: `.docx`/`.pptx` fail loudly and
get noticed; a PDF succeeds and costs.

`permissionDecision: "ask"`, never `deny` — a scanned or layout-dependent PDF genuinely needs a
native read; converting it would mutilate it. This hook interrupts and names the cheaper path
(`document-to-md`, `pc-core`); it does not decide in the caller's place.

Python 3.12+, stdlib only. A hook failure must never block a tool call → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

COSTLY_SUFFIXES = {".pdf", ".html", ".xlsx", ".docx", ".pptx"}


def target_suffix(tool_input: dict) -> str:
    """The lowercased suffix of the file a `Read` call targets, or `""` if none."""
    return Path(tool_input.get("file_path", "")).suffix.lower()


def decide(event: dict) -> dict | None:
    """Ask before a costly native read, else None (let it proceed)."""
    if event.get("tool_name") != "Read":
        return None
    suffix = target_suffix(event.get("tool_input") or {})
    if suffix not in COSTLY_SUFFIXES:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"Reading a `{suffix}` natively loads the whole file (or its raw markup) into "
                "context. If this is a normal document — not a scanned or layout-dependent PDF — "
                "convert it first with the `document-to-md` skill (`pc-core`) instead. Confirm to "
                "read it natively anyway."
            ),
        }
    }


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
