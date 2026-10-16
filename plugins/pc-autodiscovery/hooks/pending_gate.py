#!/usr/bin/env python3
"""pending_gate — PreToolUse hook (matcher: *).

Stops the agent's next tool call whenever pending_user_action has queued
something only the user can do — a slash command the CLI parses before a
turn. The stop carries every queued message, so the request is a hard block
instead of a notification that scrolls away while the agent keeps working.

Two strengths, keyed on *which* action is pending:

- **`deny`** for `reload-plugins` only. The permission `ask` UI only
  offers yes/no — the user cannot type the slash command into it — so an
  `ask` here is answered "yes", the agent runs the turn on the stale plugin
  set, and the command typed afterwards waits for the turn to end. `deny`
  instead forces the turn to end now. A `/goal` never denies: a locked turn
  feeds the goal's own Stop hook, which relaunches a turn that can only
  repeat the request. The key is **peeked, not
  popped**, so every tool call for the rest of the turn is denied too. To not
  wedge the session forever, a deny drops a latch file; the `UserPromptSubmit`
  hook (clear_pending_queue.py) sees the latch and empties the queue on the
  user's next message, which is the unblock.
- **`ask`** for anything else (`rename`): cosmetic, nothing breaks silently
  if the agent continues, so a single approve-to-continue is enough. This
  path pops the queue — it fires once per batch.

The latch is what keeps a `reload-plugins` queued at *SessionStart* from being
wiped by the user's first prompt before the gate ever runs: no deny yet → no
latch → the queue survives to the first tool call, and the nudge surfaces.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pending_user_action as _pua
from pending_user_action import pending, surface

# Clés dont l'action en attente, non faite, rend la suite du tour fausse et
# pas seulement mal informée — voir le docstring du module.
DENY_KEYS = frozenset({"reload-plugins"})


def _latch_path(session_id: str) -> Path:
    return _pua.STATE_DIR / f"{session_id}.deny-latch"


def latch(session_id: str) -> None:
    """Record that this turn issued a `deny` — clear_pending_queue reads it."""
    _pua.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _latch_path(session_id).touch()


def is_latched(session_id: str) -> bool:
    """Whether a `deny` has been issued since the last queue clear."""
    return _latch_path(session_id).exists()


def unlatch(session_id: str) -> None:
    """Drop the latch — a no-op when it was never set."""
    _latch_path(session_id).unlink(missing_ok=True)


def run(session_id: str) -> dict:
    """The hook's decision for `session_id`: `{}` when the queue is empty; a
    PreToolUse `deny` (queue left intact, latch set) when a `DENY_KEYS` action
    is pending; a PreToolUse `ask` (queue emptied) otherwise."""
    queued = pending(session_id)
    if not queued:
        return {}

    if DENY_KEYS.intersection(queued):
        latch(session_id)
        reason = (
            "⛔ Action requise — ce tour ne peut pas continuer :\n"
            + "\n".join(f"  • {m}" for m in queued.values())
            + "\n\nTermine ce tour sans relancer d'outil, fais l'action, "
            "puis reprends au message suivant."
        )
        return _decision("deny", reason)

    messages = surface(session_id)
    reason = (
        "⏸️ Action requise avant de continuer :\n"
        + "\n".join(f"  • {m}" for m in messages)
        + "\n\nFais-la, puis approuve — ou approuve pour continuer sans."
    )
    return _decision("ask", reason)


def _decision(permission: str, reason: str) -> dict:
    """A PreToolUse hook payload carrying `permission` and `reason`."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission,
            "permissionDecisionReason": reason,
        }
    }


def main_with_payload(payload: dict) -> None:
    print(json.dumps(run(payload["session_id"])))


def main() -> None:
    # Fail open, always. This is a matcher-less PreToolUse hook — a raised
    # exception here would block *every* tool call. A bad payload, a broken
    # import, a missing queue file: none of that is worth locking the session.
    try:
        main_with_payload(json.load(sys.stdin))
    except Exception:
        print("{}")


if __name__ == "__main__":
    main()
