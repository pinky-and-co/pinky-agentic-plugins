#!/usr/bin/env python3
"""SessionStart hook — freeze an orchestrator session to the kanban boards.

Two session roles, two ways their write-scope is set:

- **Worker** — a session whose cwd is inside a git repo, taking cards and editing code. Its
  freeze is a per-session toggle: `/chantier` step 7 calls `permission-change freeze <repos>
  <board roots>` at card intake. Nothing here touches it.
- **Orchestrator** — a session opened **outside any git repo** (workspace root, a repo group),
  for board curation and spawning workers; it never edits code. It has no card intake to hang a
  freeze on, so this hook gives it one at SessionStart: when the cwd is in no git repo AND at
  least one kanban board is mounted, it writes the boards' card + agent-log directories into
  `.guard-state.json` as the `freeze` whitelist and turns `careful` on.

The role is derived, not declared — the git-repo boundary already *is* the worker/orchestrator
boundary (the grill's own framing: "the session is bound to the repo"). No cwd → not in a repo,
board mounted → orchestrator; anything else → this hook does nothing. Lift a freeze you do not
want with `permission-change freeze off` (it returns next SessionStart).

The freeze lands in the entry of the starting session (`session_id`), like every guard setting —
`guard.py`'s PreToolUse enforcement and its `status` output read that entry and are untouched.

Python 3.11+, stdlib only. A hook failure must never block a session → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_HOOKS_DIR.parent / "skills" / "permission-change" / "scripts"))
sys.path.insert(0, str(_HOOKS_DIR / "guard-state"))
import guard  # noqa: E402  — seulement pour `project_root`, l'unique endroit qui résout la racine
import pc_kanban_guard_state as gs  # noqa: E402
import stop_handoff  # noqa: E402  — the one board resolver (find_kanbans + agent_log_dir)


def in_git_repo(path: Path) -> bool:
    """True when `path` sits inside a git working tree — that session is a worker, not an
    orchestrator.

    A missing/failed `git` degrades to False → the session is treated as an orchestrator and
    frozen: the safe direction here (a wrongly-frozen worker recovers with
    `permission-change freeze off`; a wrongly-unfrozen orchestrator does not notice)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True,
            timeout=2,
            check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def board_scope(start: Path) -> list[str]:
    """Every mounted board's card directory + agent-log directory as strings — the whole
    writable surface an orchestrator session is allowed. Empty when no board is mounted."""
    scope: list[str] = []
    for kanban in stop_handoff.find_kanbans(start):
        scope.append(str(kanban))
        scope.append(str(stop_handoff.agent_log_dir(kanban)))
    return scope


def apply_orchestrator_scope(payload: dict) -> str | None:
    """Freeze the guard state to the board scope when this session is an orchestrator (cwd in no
    git repo, at least one board mounted). Returns a one-line summary of what changed, or None
    when nothing did (a worker, no board mounted, no session id — reported on stderr — or the
    scope is already in place)."""
    cwd = Path(payload.get("cwd") or os.getcwd())
    if in_git_repo(cwd):
        return None

    scope = board_scope(cwd)
    if not scope:
        return None  # no board mounted → not an orchestrator context, leave the session alone

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        print(
            "orchestrator_freeze: SessionStart without a session_id, no freeze applied — "
            f"payload keys: {sorted(payload)}",
            file=sys.stderr,
        )
        return None  # sans session, aucune entrée où poser le gel

    path = gs.state_path(guard.project_root(payload))
    state = gs.load_session(path, session_id)
    if bool(state.get("careful")) and sorted(state.get("freeze") or []) == sorted(
        scope
    ):
        return None  # already in place — no rewrite, no mtime churn

    gs.update_session(
        path, session_id, lambda entry: entry.update(careful=True, freeze=scope)
    )
    return (
        f"orchestrator scope applied: careful on · freeze → {len(scope)} board dir(s)"
    )


def main() -> int:
    """Read the SessionStart event from stdin, freeze the session when it is an orchestrator, and
    always emit the SessionStart envelope. Never raises."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        payload = {}
    try:
        if payload.get("hook_event_name") in (None, "SessionStart"):
            summary = apply_orchestrator_scope(payload)
            if summary:
                print(summary, file=sys.stderr)
    except Exception as exc:  # a hook must never break a session on its own bug
        print(f"orchestrator_freeze: skipped ({exc!r})", file=sys.stderr)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
