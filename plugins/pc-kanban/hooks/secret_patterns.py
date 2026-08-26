#!/usr/bin/env python3
"""secret_patterns — the one secret-shape detector, shared by every hook that needs it.

Extracted from `stop_handoff.py` (2026-08-19): `privacy_egress.py` needed the same detection and
was importing it sideways from a hook it has nothing to do with (kanban handoff vs read-time
privacy) via a same-directory `sys.path` hack — an invisible coupling between two unrelated
concerns. Never declared in `hooks.json` and never invoked directly — a pure dependency, same role
as `policy_lib.py`.

`stop_handoff.py` itself still carries its own copy for now — it is distributed standalone into
every repo's `.claude/hooks/` and must not import an installed plugin's package (see its own
docstring). `1-todo/migrer-stop-handoff-vers-pc-core-sessionend.md` adopts this module and drops
that copy once `stop_handoff.py` joins `pc-core/hooks/` as a sibling.
"""

from __future__ import annotations

import re

# Un match keyword=value dont la valeur est une lecture d'environnement, pas un littéral — exclu des
# deux natures "* = value" ci-dessous via un negative lookahead juste après le `[:=]`.
_ENV_READ = r"os\.environ|os\.getenv\(|process\.env\.|System\.getenv\("

# Each entry is (kind, pattern). The `kind` is the human reason a caller surfaces alongside a line
# number, so a reader hitting a false positive knows WHICH shape matched, not just where.
_SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (
        "secret keyword = value",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|bearer)\b[^\S\n]*[:=][^\S\n]*"
            rf"(?!(?:{_ENV_READ}))\S+"
        ),
    ),
    (
        "high-entropy base64 blob",
        re.compile(
            r"\b(?=[A-Za-z0-9+]*[a-z])(?=[A-Za-z0-9+]*[A-Z])(?=[A-Za-z0-9+]*[0-9])"
            r"[A-Za-z0-9+]{32,}={0,2}\b"
        ),
    ),
    (
        "API-key prefix",
        re.compile(r"\b(?:gh[ps]_|xox[baprs]-|sk[-_]|AKIA)[-_A-Za-z0-9]{16,}\b"),
    ),
    ("credentials in URL", re.compile(r"https?://[^/\s:@]+:[^/\s@]+@\S+")),
    (
        "PEM private-key header",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "JWT",
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"),
    ),
    (
        "credential keyword = value",
        re.compile(
            r"(?i)\b(?:aws_secret_access_key|client_secret|private_key|access_token|auth_token|refresh_token|api_secret)[^\S\n]*[:=][^\S\n]*"
            rf"(?!(?:{_ENV_READ}))\S+"
        ),
    ),
]


def scrub(text: str) -> str:
    """Redact secret-shaped substrings before persisting or forwarding `text`."""
    for _kind, pat in _SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def secret_kinds(text: str) -> list[str]:
    """Distinct kinds (human labels) of secret shape found in `text`, in pattern order; empty = clean."""
    return [kind for kind, pat in _SECRET_PATTERNS if pat.search(text)]
