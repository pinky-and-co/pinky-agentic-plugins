#!/usr/bin/env python3
"""
check_forbidden_keywords.py — Block confidential employer references.

Source of truth: the ACTIVE profile, located via the PINKY_PROFILE env var (the same
variable the MCP server and the Phase 3 runner use). The profile declares the keywords:

    forbidden_keywords:                       # inline, generic, non-sensitive terms
      - foo
      - bar
    forbidden_keywords_file: ~/.pinky/...txt  # pointer to a PRIVATE, out-of-repo list

The VALUES never live in git — sensitive employer terms go in the out-of-repo file the
pointer references. The inline list is for generic terms only.

Resolution order:
  1. PINKY_PROFILE set → read `forbidden_keywords` (inline) + `forbidden_keywords_file`.
  2. otherwise → legacy fallback: ~/.pinky/forbidden_keywords.txt (backward compat).
  3. nothing found → skip silently (exit 0), so CI without config passes unblocked.

Matching is case-insensitive and whole-word (to avoid false positives on substrings).

Stdlib only — this hook is distributed into each consumer repo and runs in its pre-commit
environment, which may not have pyyaml. The profile is parsed with a minimal line reader,
not a YAML library.

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

PROFILE_ENV = "PINKY_PROFILE"
_LEGACY_CONFIG = Path.home() / ".pinky" / "forbidden_keywords.txt"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1]
    return value.strip()


def _read_keyword_file(path: Path) -> list[str]:
    """One keyword per line; blank lines and # comments ignored."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def _parse_profile_keywords(profile_path: Path) -> list[str]:
    """Extract forbidden keywords from a profile.yaml without a YAML dependency.

    Handles the two top-level keys this hook consumes:
      - forbidden_keywords:  block list (``- item`` lines), inline ``[a, b]``, or empty
      - forbidden_keywords_file:  scalar path (``~`` expanded), read line-per-keyword
    Only top-level (column-0) keys are considered, to avoid matching nested look-alikes.
    """
    try:
        lines = profile_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    keywords: list[str] = []
    in_list = False
    for raw in lines:
        # Continue an open block list until a dedent / next key.
        if in_list:
            m = re.match(r"\s+-\s+(.+)$", raw)
            if m:
                kw = _unquote(m.group(1))
                if kw:
                    keywords.append(kw)
                continue
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            in_list = False  # non-list, non-blank line ends the block

        # Top-level keys only (no leading whitespace, not a comment).
        if raw.startswith((" ", "\t", "#")):
            continue

        m = re.match(r"forbidden_keywords:\s*(.*)$", raw)
        if m:
            rest = m.group(1).strip()
            if rest == "":
                in_list = True  # block form continues on following indented lines
            elif rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                if inner:
                    keywords.extend(
                        _unquote(x) for x in inner.split(",") if _unquote(x)
                    )
            # rest == "[]" or other scalar → no inline keywords
            continue

        m = re.match(r"forbidden_keywords_file:\s*(.+)$", raw)
        if m:
            file_val = _unquote(m.group(1))
            if file_val:
                fpath = Path(file_val).expanduser()
                if fpath.exists():
                    keywords.extend(_read_keyword_file(fpath))
            continue

    return keywords


def load_keywords() -> list[str]:
    """Resolve keywords from the active profile, else the legacy file, else none."""
    profile_arg = os.environ.get(PROFILE_ENV)
    if profile_arg:
        profile_path = Path(profile_arg).expanduser()
        if profile_path.exists():
            keywords = _parse_profile_keywords(profile_path)
            # dedupe, preserve order
            return list(dict.fromkeys(keywords))

    # Legacy fallback — backward compatible with pre-profile setups.
    return _read_keyword_file(_LEGACY_CONFIG)


def _config_hint() -> str:
    profile_arg = os.environ.get(PROFILE_ENV)
    if profile_arg:
        return f"{PROFILE_ENV}={profile_arg} (forbidden_keywords / forbidden_keywords_file)"
    return str(_LEGACY_CONFIG)


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
