#!/usr/bin/env python3
"""
check_forbidden_keywords.py — Block confidential employer references.

Source of truth: a plain newline-separated keyword file, out of git — sensitive employer terms
never live in this repo. Default path ``~/.pinky/forbidden_keywords.txt``; override with the
``PINKY_FORBIDDEN_KEYWORDS_FILE`` env var.

Nothing found (no file at the resolved path) → skip silently (exit 0), so CI without config passes
unblocked.

Matching is case-insensitive and whole-word (to avoid false positives on substrings).

Stdlib only — this hook is distributed into each consumer repo and runs in its pre-commit
environment.

Usage (pre-commit local hook):
  entry: python scripts/hooks/check_forbidden_keywords.py
  language: system
  pass_filenames: true
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Windows' default console codepage (cp1252, not UTF-8) can't encode this hook's ✓/⚠ output and
# crashes with UnicodeEncodeError (found by the windows-latest CI job, CHANTIER0001
# portage-windows-du-moteur). Same fix as setup/__init__.py, applied here because this script runs
# standalone (invoked directly by pre-commit) and never imports that package.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

KEYWORDS_FILE_ENV = "PINKY_FORBIDDEN_KEYWORDS_FILE"
_DEFAULT_CONFIG = Path.home() / ".pinky" / "forbidden_keywords.txt"


def _read_keyword_file(path: Path) -> list[str]:
    """One keyword per line; blank lines and # comments ignored."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def _config_path() -> Path:
    override = os.environ.get(KEYWORDS_FILE_ENV)
    return Path(override).expanduser() if override else _DEFAULT_CONFIG


def load_keywords() -> list[str]:
    """Resolve keywords from the configured file, deduped, order preserved."""
    return list(dict.fromkeys(_read_keyword_file(_config_path())))


def _config_hint() -> str:
    return str(_config_path())


def _make_pattern(keywords: list[str]) -> re.Pattern[str] | None:
    if not keywords:
        return None
    # whole-word match, case-insensitive
    escaped = [re.escape(kw) for kw in keywords]
    return re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)


def _looks_binary(path: Path) -> bool:
    """Heuristic: a NUL byte in the first 8KB means binary (same rule git/grep use)."""
    try:
        with path.open("rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def check_file(path: Path, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    if _looks_binary(path):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    violations = []
    for i, line in enumerate(text.splitlines(), 1):
        m = pattern.search(line)
        if m:
            violations.append((i, m.group(0)))
    return violations


def _is_disabled() -> bool:
    disabled = os.environ.get("PINKY_DISABLED_HOOKS", "")
    return Path(__file__).stem in {h.strip() for h in disabled.split(",") if h.strip()}


def main() -> int:
    if _is_disabled():
        return 0
    keywords = load_keywords()
    if not keywords:
        return 0

    pattern = _make_pattern(keywords)
    assert pattern is not None

    failed = False
    for filepath in sys.argv[1:]:
        path = Path(filepath)
        for line, keyword in check_file(path, pattern):
            print(f"❌ [FORBIDDEN] {filepath}:{line}: found '{keyword}'")
            failed = True

    if failed:
        print(f"   💡 Employer reference detected. Check {_config_hint()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
