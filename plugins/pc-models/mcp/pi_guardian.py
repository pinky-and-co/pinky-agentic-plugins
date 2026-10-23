#!/usr/bin/env python3
"""Kill a `pi` process group when the MCP server that started it dies without cleaning up.

A `SIGKILL` leaves the server no chance to run its own cleanup, and each `pi` lives in its own
session (`start_new_session`), so nothing else reaches it. `call_scope.CallScope.adopt` starts one
of these beside every `pi`. It is a child of the server, so it is re-parented the moment the server
dies, which is what it watches for. Stdlib only, so it runs under any interpreter.

Usage: `pi_guardian.py <server pid> <process group>`
"""

from __future__ import annotations

import os
import signal
import sys
import time

POLL_SECONDS = 0.5


def main(argv: list[str]) -> int:
    """Wait until the group is gone or its owner is, and kill the group in the second case.

    Args:
        argv: the program name, the server's pid and the process group to guard.

    Returns:
        0 once the group ended by itself, belongs to someone else, or was killed here.
    """
    server, group = int(argv[1]), int(argv[2])
    while True:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            # Le pid a été réutilisé par un groupe qui n'est pas le nôtre : ne rien viser.
            return 0
        if os.getppid() != server:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
