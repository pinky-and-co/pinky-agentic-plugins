#!/usr/bin/env python3
"""pre-commit hook — every vendored file must match its checksum in `.vendor-manifest.json`.

`.vendor-manifest.json` (repo root) is written by `pinky distribute` — path -> sha256 of the
content that was copied in from the pinky-bootstrap source of truth. A mismatch means the local
copy was hand-edited instead of the source: it will be silently overwritten by the next
distribution, so catch it at commit time rather than let the edit evaporate later.

Usage:
    python3 check_vendor_checksum.py   # no args -> scan the whole repo, pass_filenames: false
"""

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
MANIFEST_NAME = ".vendor-manifest.json"


def load_manifest() -> dict[str, str]:
    """Return the path -> sha256 manifest, or `{}` if this repo has none."""
    manifest_path = REPO_ROOT / MANIFEST_NAME
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def main() -> int:
    """Recompute each manifest entry's sha256, print a line per path, exit 1 on any mismatch."""
    manifest = load_manifest()
    if not manifest:
        print("No .vendor-manifest.json — nothing to check.")
        return 0

    mismatched = []
    for rel_path, expected in sorted(manifest.items()):
        path = REPO_ROOT / rel_path
        if not path.exists():
            print(f"[MISSING] {rel_path}")
            mismatched.append(rel_path)
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            print(f"[DRIFT] {rel_path}")
            mismatched.append(rel_path)
        else:
            print(f"[OK] {rel_path}")

    if mismatched:
        print(
            f"{len(mismatched)}/{len(manifest)} vendored file(s) drifted from their source — "
            "edit the source in pinky-bootstrap's hygiene/vendor/, then `pinky distribute`, "
            "never this copy directly."
        )
        return 1

    print(f"{len(manifest)} vendored file(s) OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
