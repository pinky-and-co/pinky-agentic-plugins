#!/usr/bin/env python3
"""pre-commit hook — every vendored file must match its checksum in `.vendor-manifest.json`.

`.vendor-manifest.json` (repo root) is written by `pinky repo distribute` — path -> sha256 of the
content that was copied in from the pinky-bootstrap source of truth. A mismatch means the local
copy was hand-edited instead of the source: it will be silently overwritten by the next
distribution, so catch it at commit time rather than let the edit evaporate later.

The manifest must be committed: git-ignored, a CI checkout never has it and this check passes on
nothing there — so an ignored manifest fails too.

Usage:
    python3 check_vendor_checksum.py   # no args -> scan the whole repo, pass_filenames: false
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from _report import Diagnostic, emit

REPO_ROOT = Path(__file__).parent.parent.parent
MANIFEST_NAME = ".vendor-manifest.json"


def load_manifest() -> dict[str, str]:
    """Return the path -> sha256 manifest, or `{}` if this repo has none."""
    manifest_path = REPO_ROOT / MANIFEST_NAME
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _manifest_is_ignored() -> bool:
    """Whether git ignores the manifest — then no CI checkout has it, and the check proves nothing."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", MANIFEST_NAME],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    """Recompute each manifest entry's sha256, print a line per path, exit 1 on any mismatch."""
    manifest = load_manifest()
    if not manifest:
        return 0
    if _manifest_is_ignored():
        emit(
            [
                Diagnostic(
                    "VENDOR",
                    "manifest is git-ignored — a CI checkout never has it, so this check "
                    "passes on nothing there",
                    MANIFEST_NAME,
                    help="remove it from .gitignore and commit it",
                )
            ]
        )
        return 1

    findings: list[Diagnostic] = []
    for rel_path, expected in sorted(manifest.items()):
        path = REPO_ROOT / rel_path
        if not path.exists():
            findings.append(Diagnostic("VENDOR", "vendored file is missing", rel_path))
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            findings.append(
                Diagnostic("VENDOR", "drifted from its source checksum", rel_path)
            )

    emit(
        findings,
        footer="edit the source in pinky-bootstrap's hygiene/vendor/, then `pinky repo distribute`, "
        "never this copy directly",
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
