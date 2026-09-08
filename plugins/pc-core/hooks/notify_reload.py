#!/usr/bin/env python3
"""notify_reload — ConfigChange hook (matcher: local_settings).

Deliberately decoupled from sync_plugin_scope.py: this hook doesn't know or
care *why* settings.local.json changed (our own `claude plugin enable -s
local` calls, a manual edit, Claude Code's own /config writer) — it only
reacts to the file having changed, and queues the one thing that's always
true when it has: `/reload-plugins` is needed for the change to take effect.

The queue is pending_user_action; pending_gate turns the next tool call into
a permission `ask` carrying it. Same `reload-plugins` key sync_plugin_scope
uses, so a settings write it triggered does not double the prompt.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pending_user_action import require


def main() -> None:
    payload = json.load(sys.stdin)
    source = payload.get("source", "local_settings")
    require(
        payload["session_id"],
        "reload-plugins",
        f"/reload-plugins  — {source} a changé, à charger pour cette session",
    )
    print("{}")


if __name__ == "__main__":
    main()
