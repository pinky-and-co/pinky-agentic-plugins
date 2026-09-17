"""Denylist-based secret filtering for the "aller" leg export.

Two separate concerns, deliberately not merged:

- **File exclusion** — a whole file never goes in the zip (`.env`, a private key, a local
  credentials file). `pc-privacy`'s `anonymize_detect`/`anonymize_apply` are the wrong tool here:
  they pseudonymize PII inside structured tabular data (CSV/JSON/Parquet/XML columns), not decide
  whether an arbitrary file should be excluded outright.
- **Path redaction** — text content (an `_AGENT_LOG` entry) that may carry a local absolute path
  is rewritten in place, the file itself still goes in the zip.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch

_DENYLIST_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*credentials*",
    "*.claude/settings.local.json",
    "*.p12",
    "*.pfx",
    "*.key",
)

_LOCAL_PATH_RE = re.compile(r"(?:/Users/[^/\s]+|/home/[^/\s]+|/root)(?:/[^\s]*)?")


def is_denied(rel_path: str) -> bool:
    """True when *rel_path* (repo-relative, POSIX separators) matches a known secret-bearing
    pattern — matched case-insensitively (a denylist that only catches `.env` and lets `.ENV`
    through is not a denylist), against the full path and against its final segment, so both a
    root-level `.env` and a nested `config/.env` are caught."""
    rel_lower = rel_path.lower()
    name_lower = rel_lower.rsplit("/", 1)[-1]
    return any(
        fnmatch(rel_lower, pattern.lower()) or fnmatch(name_lower, pattern.lower())
        for pattern in _DENYLIST_PATTERNS
    )


def redact_local_paths(text: str) -> str:
    """Replace a local machine-absolute path (`/Users/<name>/...`, `/home/<name>/...`, `/root/...`)
    with a redaction marker — the class of leak an `_AGENT_LOG` entry or a card body can carry (a
    handoff block, a repro command) without being a secret file itself."""
    return _LOCAL_PATH_RE.sub("<redacted-local-path>", text)
