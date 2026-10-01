#!/usr/bin/env python3
"""load_secrets — resolve the `keys` declared in `settings.pinky.json` into `{VAR: value}`.

Reads the `keys` array from `.claude/settings.pinky.json` (ascending resolution — see
`policy_lib.py`, the same mechanism `model_pool`/`branch_workflow` already use). Keys are resolved
on demand, only inside the process that needs them (`secret_env` in `model_adversarial.py`, handed
straight to a `pi` subprocess's own `env=`) — never exported to a session-wide env, where any tool
that dumps it (`pgrep -fl`, `env`, a command's own argv expansion) would read every secret for the
session's whole lifetime.

Each `keys` entry: `{"reader": "...", "env": "VAR_NAME", ...reader-specific fields}`. `reader`
dispatches to a wiring function below; each names its own fields. One is wired:

- `macos_keychain` (`{"service": ...}`) — the classic keychain, Trousseau d'accès,
  `security find-generic-password`; NOT the Passwords app, which stores in a structurally
  different, unreachable format — see `docs/how-to/setup-declarative-secrets.md` (pinky-bootstrap)
  for why. Every secret goes here: never write a credential in `settings.pinky.json` itself.

An unwired reader (`aws_secretsmanager`, an unknown name, ...) fails loud on stderr and leaves that
variable unset — never a silent skip, never a fabricated value.

Resolution must never hang: the timeout on every subprocess call in `keychain.py` guards it — a
timeout, a missing item, or a locked keychain still degrades to "skip this entry, log why", never a
hang, including in a scheduled/headless session with no physical user to unlock a keychain or
answer a Touch ID prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from keychain import read_macos_keychain as _read_keychain  # noqa: E402
from policy_lib import load_policies, resolve_repo_root  # noqa: E402


def read_macos_keychain(entry: dict[str, Any]) -> str | None:
    """Adapt a `keys` entry (`{"service": ...}`) to the shared keychain reader."""
    service = entry.get("service")
    if not service:
        print("[load_secrets] macos_keychain entry missing 'service'", file=sys.stderr)
        return None
    return _read_keychain(service)


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


def resolve_env(keys: list[dict[str, Any]]) -> dict[str, str]:
    """`{VAR: value}` for every entry whose reader resolved a value — every reader, keychain
    included: this function itself does no filtering by reader.

    Called by pc-core's MCP server (`model_adversarial.secret_env`): it hands the resolved values to
    each `pi` call it makes, on demand, never into a session-wide env.

    Args:
        keys: the `keys` array of `settings.pinky.json`.

    Returns:
        The resolved variables, in `keys` order; unknown readers and unresolved values are
        skipped with a line on stderr.
    """
    env: dict[str, str] = {}
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
            env[env_name] = value
    return env
