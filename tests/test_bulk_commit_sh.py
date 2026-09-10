#!/usr/bin/env python3
"""Runs the bash suite `plugins/pc-git/hooks/branch-policy/test_bulk_commit.sh` under pytest.

`bulk_commit.sh` (diff grouping, CI watch, `push_with_recovery`, parallel batches) has no Python
tests — its only coverage is that bash suite, which `scripts/hooks/run-tests` never invoked
(pytest + JS only). A regression in it passed every gate. This wrapper closes that: exit 0 of the
bash suite becomes a pytest assertion, so CI blocks on it.

**CI-only.** The bash suite spins up real git repos and stub CLIs and takes ~45s. Running it on
every local commit through the `run-tests` pre-commit hook is too heavy; the merge gate is the
`pull_request` CI run, where `CI` is set. Locally, run the suite directly:
`bash plugins/pc-git/hooks/branch-policy/test_bulk_commit.sh`.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-git"
    / "hooks"
    / "branch-policy"
    / "test_bulk_commit.sh"
)


@pytest.mark.skipif(
    not os.environ.get("CI"),
    reason="~45s bash suite — run it directly outside CI, this wrapper is the merge gate",
)
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bulk_commit_shell_suite() -> None:
    """The bash suite exits 0 — its own `check` failures already print a readable diff."""
    result = subprocess.run(
        ["bash", str(SUITE)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"test_bulk_commit.sh failed (exit {result.returncode}):\n"
        f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
    )
