#!/usr/bin/env python3
"""clear_pending_queue — UserPromptSubmit hook.

The unblock for pending_gate's `deny` path: a `DENY_KEYS` action is peeked,
never popped, so once queued it denies *every* tool call until something
clears it. The user's next message is that something — conditional on the
latch pending_gate drops when it actually issues a deny, so a
`reload-plugins` queued at SessionStart, before the gate has run, is *not*
wiped by the user's first prompt: it survives to the first tool call and the
nudge surfaces. Once the gate has denied at least once, the next prompt
clears the whole queue and the latch.

Entries queued with `surfaced=True` skip that latch condition — resolved on
every prompt regardless, via `clear_surfaced()`. The flag is the caller's own
choice (`require(..., surfaced=True)`), never a key name this hook checks: a
source only sets it when the message was already shown to the user in-band
(the pasteable `/goal`/`/rename` line a response emits), so it has nothing
left to surface once the user's next message arrives. Without this, a
session that ends its turn right after queuing a surfaced action (no tool
call, so no deny, so no latch) denies the next turn's first — unrelated —
tool call once, purely because the queue was never told the action already
ran.

Fail open: a queue that fails to clear is a nuisance, never a reason to
reject the prompt.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pending_gate import is_latched, unlatch
from pending_user_action import clear_all, clear_surfaced


def main() -> None:
    try:
        session_id = json.load(sys.stdin)["session_id"]
        clear_surfaced(session_id)
        if is_latched(session_id):
            clear_all(session_id)
            unlatch(session_id)
    except Exception:
        pass
    print("{}")


if __name__ == "__main__":
    main()
