#!/usr/bin/env python3
"""PreToolUse — ask before writing a kanban card that is still being framed.

Fires on `Edit`/`Write`/`MultiEdit`/`NotebookEdit` targeting `_KANBAN/CHANTIER*/<column>/*.md` —
the same four-tool matcher `branch_guard.py` uses for the same reason: a card-worker agent's
`tools:` carries `MultiEdit`/`NotebookEdit` alongside `Edit`/`Write`, so a matcher missing either
is a gap those tools walk straight through. The rule depends on the card's column (skill
`kanban`'s column lifecycle):

| column                    | write                                                   |
|---------------------------|---------------------------------------------------------|
| `0-ideas`                 | `ask` — the idea is posed, not worked                   |
| `1-todo`                  | `ask` — the framing pass is the user's to validate      |
| `2-selected-for-dev`      | `ask` — the briefing pass likewise                      |
| `3-in-progress`, `4-done` | autonomous — ticking `## Done when`, logging decisions  |

While a card is being framed, rewriting it after every answer is noise: each write goes through
the user, who approves once the framing is ripe. Creating a card counts as a write — a `Write` to
a path that does not exist yet is asked about like any other. Once engaged, the card is the work's
own log and is kept up to date without asking.

Only `ask` — never `deny` — and never on a read. A path outside `_KANBAN` or not matching a
recognized column gets no decision at all: no column, no rule.

Python 3.12+, stdlib only. A hook failure must never block a tool call → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import re
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

GUARDED_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# One capture group: the column directory name, straight from the card's own path — no guessing.
_COLUMN_RE = re.compile(
    r"_KANBAN[/\\]CHANTIER[^/\\]*[/\\]([0-4]-[a-z-]+)[/\\][^/\\]*\.md$"
)

# True : toute écriture est soumise à l'utilisateur. False : l'agent tient la card à jour seul.
# Une colonne absente d'ici (aucune, les cinq y sont) passerait sans décision.
_COLUMN_ASK = {
    "0-ideas": True,
    "1-todo": True,
    "2-selected-for-dev": True,
    "3-in-progress": False,
    "4-done": False,
}

_REASON = (
    "card-discipline: this card is still being framed — write it once the framing is ripe, "
    "not after every answer."
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


def decide(event: dict) -> dict | None:
    """Core hook decision: `ask` on a guarded write to a card still being framed, else None.

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
        return None  # card engagée — l'agent la tient à jour seul
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
