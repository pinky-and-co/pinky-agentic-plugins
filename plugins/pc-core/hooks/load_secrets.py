#!/usr/bin/env python3
"""load_secrets — SessionStart hook that exports declared non-secret keys as environment variables.

Reads the `keys` array from `.claude/settings.pinky.json` (ascending resolution — see
`policy_lib.py`, the same mechanism `model_pool`/`branch_workflow` already use). At `SessionStart`,
only `literal` entries are appended as `export <env>=<value>` lines to `$CLAUDE_ENV_FILE`, which
Claude Code sources before every Bash command — every session's env is otherwise readable by any
tool that dumps it (`pgrep -fl`, `env`, a command's own argv expansion), so a secret has no business
sitting there for the session's whole lifetime. Append, not overwrite — a later `CwdChanged`/another
`SessionStart` hook may also write to this file; truncating it would erase a sibling hook's
contribution.

Each `keys` entry: `{"reader": "...", "env": "VAR_NAME", ...reader-specific fields}`. `reader`
dispatches to a wiring function below; each names its own fields. Two are wired:

- `macos_keychain` (`{"service": ...}`) — the classic keychain, Trousseau d'accès,
  `security find-generic-password`; NOT the Passwords app, which stores in a structurally
  different, unreachable format — see `docs/how-to/setup-declarative-secrets.md` (pinky-bootstrap)
  for why. **Every secret goes here.** Declared the same way as a `literal` entry, but never
  exported at boot: resolved on demand instead, only inside the process that needs it (`secret_env`
  in `model_adversarial.py`, handed straight to a `pi` subprocess's own `env=`).
- `literal` (`{"value": ...}`) — the value sits in `settings.pinky.json` itself, in clear text on
  disk. **Never a secret, whatever the convenience**: for an account id, a tenant, an endpoint —
  something already public in a `base_url` or a log. A credential in a `literal` entry defeats the
  only reason the keychain reader exists. The one reader kind safe to export for the whole session,
  and the only one `resolve_env_exports` (below) resolves.

An unwired reader (`aws_secretsmanager`, ...) fails loud on stderr and leaves that variable unset —
never a silent skip, never a fabricated value — wherever it is actually resolved (on demand via
`secret_env`, or at boot for a `literal` entry). A non-`literal` entry is filtered out before
dispatch on the `SessionStart` path itself (`resolve_env_exports`), so an unwired reader declared
there produces no stderr line at boot; it still fails loud the moment something calls
`secret_env()` for it.

This hook must never hang: it runs on every session start, including a scheduled/headless one with
no physical user to unlock a keychain or answer a Touch ID prompt. Resolving only `literal` entries
at boot already removes the keychain/Touch-ID prompt from this path entirely; the timeout on every
subprocess call in `keychain.py` guards the on-demand path (`secret_env`) instead — a timeout, a
missing item, or a locked keychain there still degrades to "skip this entry, log why", never a hang.

Registered as a `SessionStart` hook in `hooks/hooks.json` with no matcher: every sub-type
(`startup`, `resume`, `clear`) must run this, since a reattached/resumed session is exactly as
likely to need its `literal` keys freshly exported as a cold start (confirmed the hard way: scoping
this to `matcher: "startup"` meant a resumed session — same session id, new harness process — never
got them).
"""

from __future__ import annotations

import shlex
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


def read_literal(entry: dict[str, Any]) -> str | None:
    """Read a `keys` entry (`{"value": ...}`) holding a non-secret value inline in the settings."""
    value = entry.get("value")
    if value is None or value == "":
        print("[load_secrets] literal entry missing 'value'", file=sys.stderr)
        return None
    return str(value)


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
    "literal": read_literal,
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
    """`export VAR=value` lines for `SessionStart` — `literal` entries only.

    Filters *before* resolving, not after: a `macos_keychain`/`aws_secretsmanager` entry must never
    reach its reader on this path, or every session start pays the keychain/Touch-ID round trip for
    a value it then throws away. Allowlisted on `literal` rather than denylisted on the secret
    readers, so a future reader defaults to "not exported at boot" until proven safe.

    Args:
        keys: the `keys` array of `settings.pinky.json`.

    Returns:
        One `export` line per `literal` entry that resolved a value, in `keys` order.
    """
    literal_keys = [entry for entry in keys if entry.get("reader") == "literal"]
    return [
        f"export {name}={shlex.quote(value)}"
        for name, value in resolve_env(literal_keys).items()
    ]


def resolve_env(keys: list[dict[str, Any]]) -> dict[str, str]:
    """`{VAR: value}` for every entry whose reader resolved a value — every reader, keychain
    included: this function itself does no filtering by reader.

    Called by pc-core's MCP server (`model_adversarial.secret_env`), whose process never receives
    `$CLAUDE_ENV_FILE`: it hands the resolved values to each `pi` call it makes, on demand, never
    into a session-wide env. `resolve_env_exports` (the `SessionStart` path) calls this too, but
    pre-filtered down to `literal` entries only.

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
