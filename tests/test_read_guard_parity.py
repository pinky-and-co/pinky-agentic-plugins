#!/usr/bin/env python3
"""Parity of the two read guards' exclusion predicates.

`privacy_egress` (pc-secrets) and `pii_guard` (pc-privacy) each carry their own `_is_excluded` —
the plugins stay import-independent — so nothing structural keeps the two copies in step. This
test is that check: on a fixed, deliberately varied set of paths, one guard must never exclude
what the other scans. Each case also pins the expected verdict, so a drift in both guards at
once fails too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-secrets" / "hooks")
)
sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "hooks")
)

import pii_guard
import privacy_egress

# With the default config dir (~/.claude): memory notes and `.claude` folders are excluded; a
# repo file, a dataset in a plain home directory and a Proton Drive kanban card are not.
DEFAULT_DIR_CASES: list[tuple[Path, bool]] = [
    (Path("~/.claude/projects/-Users-x-demo/memory/note.md").expanduser(), True),
    (Path("~/.claude/settings.json").expanduser(), True),
    (Path("/Users/x/dev/demo/.claude/settings.pinky.json"), True),
    (Path("/Users/x/dev/demo/src/main.py"), False),
    (Path("~/work/clients.csv").expanduser(), False),
    (
        Path(
            "~/Library/CloudStorage/ProtonDrive-x@example.me-folder"
            "/shared/_KANBAN/3-in-progress/card.md"
        ).expanduser(),
        False,
    ),
]

# With CLAUDE_CONFIG_DIR pointing outside any `.claude` folder: the silo is excluded wherever
# it points, and only it.
CONFIG_DIR_CASES: list[tuple[Path, bool]] = [
    (Path("/opt/agent-silo/settings.json"), True),
    (Path("/opt/agent-silo/projects/-Users-x-demo/memory/note.md"), True),
    (Path("/opt/other/creds.txt"), False),
]


@pytest.mark.parametrize(("path", "excluded"), DEFAULT_DIR_CASES)
def test_with_the_default_config_dir(path: Path, excluded: bool, monkeypatch) -> None:
    """Without `CLAUDE_CONFIG_DIR`, both guards fall back to `~/.claude` and agree exactly."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert privacy_egress._is_excluded(path) is excluded
    assert pii_guard._is_excluded(path) is excluded


@pytest.mark.parametrize(("path", "excluded"), CONFIG_DIR_CASES)
def test_with_a_resolved_config_dir(path: Path, excluded: bool, monkeypatch) -> None:
    """Both guards follow `CLAUDE_CONFIG_DIR` wherever it points, `.claude` component or not."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/agent-silo")
    assert privacy_egress._is_excluded(path) is excluded
    assert pii_guard._is_excluded(path) is excluded
