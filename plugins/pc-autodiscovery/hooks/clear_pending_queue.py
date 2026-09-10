#!/usr/bin/env python3
"""clear_pending_queue — UserPromptSubmit hook.

The unblock for pending_gate's `deny` path: a `DENY_KEYS` action is peeked,
never popped, so once queued it denies *every* tool call until something
clears it. The user's next message is that something.

Conditional on the latch pending_gate drops when it actually issues a deny —
so a `reload-plugins` queued at SessionStart, before the gate has run, is
*not* wiped by the user's first prompt: it survives to the first tool call
and the nudge surfaces (FR-0025-25). Once the gate has denied at least once,
the next prompt clears the queue and the latch.

Fail open: a queue that fails to clear is a nuisance, never a reason to
reject the prompt.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pending_gate import is_latched, unlatch
from pending_user_action import clear_all


def main() -> None:
    try:
        session_id = json.load(sys.stdin)["session_id"]
        if is_latched(session_id):
            clear_all(session_id)
            unlatch(session_id)
    except Exception:
        pass
    print("{}")


if __name__ == "__main__":
    main()
