#!/usr/bin/env python3
"""PreToolUse — ask before editing a kanban card outside its column's own editing window.

Fires on `Edit`/`Write`/`MultiEdit`/`NotebookEdit` targeting `_KANBAN/CHANTIER*/<column>/*.md` —
the same four-tool matcher `branch_guard.py` uses for the same reason: a card-worker agent's
`tools:` carries `MultiEdit`/`NotebookEdit` alongside `Edit`/`Write`, so a matcher missing either
is a gap those tools walk straight through. A card's editing window is closed and depends on its
column (skill `kanban`'s column lifecycle):

| column               | editing                                            |
|-----------------------|----------------------------------------------------|
| `0-ideas`              | none — the idea is posed, not worked               |
| `1-todo`               | the framing pass: what, why, `## Done when`        |
| `2-selected-for-dev`   | the briefing pass: card-specific agent instructions|
| `3-in-progress`, `4-done` | `ask`                                            |

Only `ask` — never `deny` — and never on a read: re-editing an engaged card's scope is not
maintenance, it is the signal it was premature. One exception carries no `ask`: every `old_string`
the call touches lies entirely inside the `<!-- auto-handoff:start ... end -->` block, a zone
`stop_handoff.py` already owns and rewrites on every `Stop` — `Edit` checks its one `old_string`,
`MultiEdit` checks *every* entry in `edits[]` (one outside the block is enough to `ask`). `Write`
replaces the whole file and `NotebookEdit` carries no `old_string` at all, so both always fall
through to the column rule. A path outside `_KANBAN` or not matching a recognized column gets no
decision at all: no column, no rule.

Python 3.12+, stdlib only. A hook failure must never block a tool call → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from stop_handoff import BLOCK_END, BLOCK_START  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

GUARDED_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# One capture group: the column directory name, straight from the card's own path — no guessing.
_COLUMN_RE = re.compile(
    r"_KANBAN[/\\]CHANTIER[^/\\]*[/\\]([0-4]-[a-z-]+)[/\\][^/\\]*\.md$"
)

# True: the column's window is closed, an edit is asked about. False: the card's own drafting
# pass for that column — nothing is enforced. A column absent here (there is none, all five of
# the board's columns are listed) would also mean "nothing is enforced".
_COLUMN_ASK = {
    "0-ideas": True,
    "1-todo": False,
    "2-selected-for-dev": False,
    "3-in-progress": True,
    "4-done": True,
}

_REASON = (
    "card-discipline: this card's column has closed its editing window — re-editing an engaged "
    "card's scope is not maintenance, it is the signal it was premature. Consider withdrawing the "
    "card instead of correcting it."
)


def _decision(reason: str) -> dict:
    """Wrap a PreToolUse permission decision in the harness-expected envelope."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def target_path(tool_name: str, tool_input: dict) -> str | None:
    """The file a guarded tool call targets (`NotebookEdit` uses `notebook_path`)."""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path")
    return tool_input.get("file_path")


def card_column(path: str) -> str | None:
    """The kanban column `path` resolves to, or None when it isn't a recognized
    `_KANBAN/CHANTIER*/<column>/*.md` card path.

    Args:
        path: a tool's target file path, absolute or relative.

    Returns:
        The column directory name (e.g. `3-in-progress`), or None.
    """
    m = _COLUMN_RE.search(path.replace("\\", "/"))
    return m.group(1) if m else None


def within_handoff_block(text: str, old_string: str) -> bool:
    """Whether an `Edit`'s `old_string` falls entirely inside the managed auto-handoff block.

    Args:
        text: the card's current full content.
        old_string: the `Edit` tool's `old_string` input.

    Returns:
        True when `old_string` is contained in the span between `BLOCK_START` and `BLOCK_END`
        (inclusive of both markers) — the one zone `stop_handoff.py` already owns. False when
        either marker is absent, malformed (end before start), or `old_string` reaches outside
        the span.
    """
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return False
    span = text[start : end + len(BLOCK_END)]
    return old_string in span


def _handoff_old_strings(tool: str, tool_input: dict) -> list[str] | None:
    """Every `old_string` a guarded call touches, or None when the tool can never qualify for
    the auto-handoff exception.

    Args:
        tool: the guarded tool name.
        tool_input: the call's `tool_input`.

    Returns:
        `[old_string]` for `Edit`; every `edits[].old_string` for `MultiEdit` (None, not a list,
        when `edits` is missing/empty/malformed — that call cannot qualify either); None for
        `Write`/`NotebookEdit`, which never carry an `old_string` to check.
    """
    if tool == "Edit":
        old_string = tool_input.get("old_string")
        return [old_string] if isinstance(old_string, str) and old_string else None
    if tool == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        old_strings: list[str] = []
        for e in edits:
            old_string = e.get("old_string") if isinstance(e, dict) else None
            if not isinstance(old_string, str) or not old_string:
                return None
            old_strings.append(old_string)
        return old_strings
    return None


def qualifies_for_handoff_exception(tool: str, tool_input: dict, path: str) -> bool:
    """Whether this call is entirely confined to the auto-handoff block — the one exception that
    carries no `ask`.

    Args:
        tool: the guarded tool name.
        tool_input: the call's `tool_input`.
        path: the card's file path (for reading its current content).

    Returns:
        True only when every `old_string` the call touches lies inside the block. `Write` and
        `NotebookEdit` always return False — see `_handoff_old_strings`.
    """
    old_strings = _handoff_old_strings(tool, tool_input)
    if old_strings is None:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return all(within_handoff_block(text, s) for s in old_strings)


def decide(event: dict) -> dict | None:
    """Core hook decision: `ask` on a guarded write outside its column's window, else None.

    Args:
        event: the parsed PreToolUse payload (`tool_name`, `tool_input`).

    Returns:
        A PreToolUse `ask` decision, or None to let the tool call proceed silently.
    """
    tool = event.get("tool_name", "")
    if tool not in GUARDED_TOOLS:
        return None
    tool_input = event.get("tool_input") or {}
    path = target_path(tool, tool_input)
    if not path:
        return None
    column = card_column(path)
    if column is None:
        return None  # not a recognized card path — nothing to enforce
    if not _COLUMN_ASK.get(column, False):
        return None  # this column's own drafting pass — nothing to enforce

    if qualifies_for_handoff_exception(tool, tool_input, path):
        return None  # the one exception: the auto-handoff zone, already stop_handoff's

    return _decision(_REASON)


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
