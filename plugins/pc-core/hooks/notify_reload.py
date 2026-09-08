#!/usr/bin/env python3
"""notify_reload — ConfigChange hook (matcher: local_settings).

Deliberately decoupled from sync_plugin_scope.py: this hook doesn't know or
care *why* settings.local.json changed (our own `claude plugin enable -s
local` calls, a manual edit, Claude Code's own /config writer) — it only
reacts to the file having changed, and says the one thing that's always true
when it has: a reload is needed for the change to take effect.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from notify_helpers import notify


def main() -> None:
    payload = json.load(sys.stdin)
    source = payload.get("source")

    message: str = (
        f"{source} changed!\n"
        "🚀 Run /reload-plugins  for the change to take effect in this session."
    )

    output = notify("Claude Code", message)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
