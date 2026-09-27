"""Shared pytest fixtures."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import model_adversarial


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    """Every test sees an empty key set — `secret_env` would read the real keychain otherwise."""
    monkeypatch.setattr(model_adversarial, "secret_env", lambda: {})


@pytest.fixture(autouse=True)
def _isolate_pool_log(monkeypatch, tmp_path):
    """A `run_pool` failure best-effort writes to `~/.claude/debug/...` — isolate `HOME` under
    `tmp_path` and drop any ambient `CLAUDE_CODE_SESSION_ID`, so any test reaching `run_pool`
    (directly, through `pi_review`, or through the CLI) never touches this machine's real
    session log."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
