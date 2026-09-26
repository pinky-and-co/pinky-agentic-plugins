#!/usr/bin/env python3
"""keychain — read one secret from the macOS classic keychain (Trousseau d'accès).

Used by `load_secrets.py` (SessionStart hook — wires declared keys into `$CLAUDE_ENV_FILE` for
Bash-land). Pool models get their keys from pi's own registry instead (`~/.pi/agent/`), which can
run the same `security find-generic-password` itself.

Classic keychain only — `security` cannot see the Passwords app's data-protection keychain
(`setup-declarative-secrets.md` in pinky-bootstrap for why). A read succeeds only while the `login`
keychain is unlocked (it unlocks with the macOS GUI session); a locked keychain, a missing item,
a missing `security` binary, or a timeout all return None — never a hang, never a raise.
"""

from __future__ import annotations

import getpass
import subprocess
import sys

SECURITY_TIMEOUT = 5


def read_macos_keychain(service: str) -> str | None:
    """The password stored under `service` for the current user, or None if it can't be read.

    None collapses every failure into one: item absent, keychain locked, `security` missing, call
    timed out. No caller needs to tell those apart — each still degrades to "this key is
    unavailable", logged on stderr.
    """
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                getpass.getuser(),
                "-s",
                service,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=SECURITY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[keychain] {service}: security call failed — {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"[keychain] {service}: not found or keychain locked (exit {result.returncode})",
            file=sys.stderr,
        )
        return None
    return result.stdout.strip()
