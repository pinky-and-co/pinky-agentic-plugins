#!/usr/bin/env python3
"""ZWS — Detection of invisible characters (zero-width spaces).

Common source: copy-paste from Outlook or Teams.
Detected characters: U+200B, U+200C, U+200D, U+FEFF.
"""

import os
import sys
from pathlib import Path

# Script autonome, n'importe pas `setup` : la console Windows (cp1252) ne sait pas encoder ✓/⚠.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ZWS = [chr(0x200B), chr(0x200C), chr(0x200D), chr(0xFEFF)]


def _looks_binary(filepath: str) -> bool:
    """Heuristic: a NUL byte in the first 8KB means binary (same rule git/grep use)."""
    try:
        with open(filepath, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def check_file(filepath: str) -> list[int]:
    found = []
    if _looks_binary(filepath):
        return found
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if any(ch in line for ch in ZWS):
                    found.append(i)
    except Exception:  # noqa: S110 — silently skip unreadable files
        pass
    return found


def _is_disabled() -> bool:
    disabled = os.environ.get("PINKY_DISABLED_HOOKS", "")
    return Path(__file__).stem in {h.strip() for h in disabled.split(",") if h.strip()}


def main() -> int:
    if _is_disabled():
        return 0
    failed = False
    for filepath in sys.argv[1:]:
        lines = check_file(filepath)
        if lines:
            print(
                f"❌ [ZWS] Invisible character (zero-width) in: {filepath} (lines: {','.join(map(str, lines))})"
            )
            print(
                "   💡 Fix: search for invisible chars in your editor "
                "(regex: [\\u200b\\u200c\\u200d\\ufeff]) and delete them. "
                "Likely source: copy-paste from Outlook or Teams."
            )
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
