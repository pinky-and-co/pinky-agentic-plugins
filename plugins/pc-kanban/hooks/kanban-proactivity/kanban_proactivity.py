#!/usr/bin/env python3
"""SessionStart hook — inject the kanban-proactivity rule into the main session.

A subagent never fires SessionStart, so this reaches the main session only. The output is a pure
function of the sibling `kanban-proactivity.md` file: no wall-clock, no nonce.

Python 3.11+, stdlib only. A hook failure must never block a session: any error exits 0 silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_RULE = Path(__file__).resolve().parent / "kanban-proactivity.md"


def build_output() -> str:
    """Build the SessionStart JSON payload carrying the rule text.

    Returns:
        The JSON document to print on stdout.
    """
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _RULE.read_text(encoding="utf-8"),
            }
        }
    )


def main() -> int:
    """Print the payload; stay silent and succeed on any error.

    Returns:
        Always 0.
    """
    try:
        out = build_output()
    except Exception:  # noqa: BLE001 — un hook ne doit jamais bloquer une session
        return 0
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
