#!/usr/bin/env python3
"""load_secrets — SessionStart hook that exports declared API keys as environment variables.

Reads the `keys` array from `.claude/settings.pinky.json` (ascending resolution — see
`policy_lib.py`, the same mechanism `model_pool`/`branch_workflow` already use) and appends
`export <env>=<value>` lines to `$CLAUDE_ENV_FILE`, which Claude Code sources before every Bash
command. Append, not overwrite — a later `CwdChanged`/another `SessionStart` hook may also write to
this file; truncating it would erase a sibling hook's contribution.

Each `keys` entry: `{"reader": "...", "env": "VAR_NAME", ...reader-specific fields}`. `reader`
dispatches to a wiring function below — `macos_keychain` is the only one wired today (the classic
keychain, Trousseau d'accès, `security find-generic-password`; NOT the Passwords app, which stores
in a structurally different, unreachable format — see `docs/how-to/setup-model-pool.md` sibling doc
for why). An unwired reader (`aws_secretsmanager`, ...) fails loud on stderr and leaves that
variable unset — never a silent skip, never a fabricated value.

This hook must never hang: it runs on every session start, including a scheduled/headless one with
no physical user to unlock a keychain or answer a Touch ID prompt. Every subprocess call carries a
short timeout; a timeout, a missing item, or a locked keychain all degrade to "skip this entry,
log why" — never a hang, never a crash that blocks the session from starting.

Wiring (.claude/settings.json):
  "hooks": {
    "SessionStart": [
      { "matcher": "startup",
        "hooks": [{"type": "command",
                    "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/load_secrets.py\""}] }
    ]
  }
"""

from __future__ import annotations

import getpass
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from policy_lib import load_policies, resolve_repo_root  # noqa: E402

SECURITY_TIMEOUT = 5


def read_macos_keychain(entry: dict[str, Any]) -> str | None:
    """`security find-generic-password` against the classic keychain — never the Passwords app."""
    service = entry.get("service")
    if not service:
        print("[load_secrets] macos_keychain entry missing 'service'", file=sys.stderr)
        return None
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
        print(f"[load_secrets] {service}: keychain call failed — {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"[load_secrets] {service}: not found in keychain (exit {result.returncode})",
            file=sys.stderr,
        )
        return None
    return result.stdout.strip()


def read_aws_secretsmanager(entry: dict[str, Any]) -> str | None:
    """Not implemented — fails loud rather than pretending to support this reader."""
    print(
        f"[load_secrets] reader aws_secretsmanager: not implemented yet "
        f"(entry for env={entry.get('env')})",
        file=sys.stderr,
    )
    return None


READERS = {
    "macos_keychain": read_macos_keychain,
    "aws_secretsmanager": read_aws_secretsmanager,
}


def load_keys(project_dir: str) -> list[dict[str, Any]]:
    """The `keys` array from `settings.pinky.json`, or `[]` if unconfigured at every level."""
    repo_root = resolve_repo_root(project_dir, None)
    settings = load_policies(repo_root)
    if settings is None:
        return []
    return settings.get("keys", [])


def resolve_env_exports(keys: list[dict[str, Any]]) -> list[str]:
    """`export VAR=value` lines for every entry whose reader resolved a value."""
    lines = []
    for entry in keys:
        reader_name = entry.get("reader")
        env_name = entry.get("env")
        reader = READERS.get(reader_name)  # type: ignore[arg-type]
        if reader is None:
            print(f"[load_secrets] unknown reader: {reader_name}", file=sys.stderr)
            continue
        if not env_name:
            print(f"[load_secrets] entry missing 'env': {entry}", file=sys.stderr)
            continue
        value = reader(entry)
        if value:
            lines.append(f"export {env_name}={shlex.quote(value)}")
    return lines


def main() -> int:
    import os

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return (
            0  # nothing to write into — silent, this hook has no other output channel
        )

    keys = load_keys(project_dir)
    if not keys:
        return 0

    lines = resolve_env_exports(keys)
    if not lines:
        return 0

    with open(env_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
