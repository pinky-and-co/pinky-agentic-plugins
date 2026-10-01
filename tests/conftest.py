"""Shared pytest fixtures."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))
sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "hooks")
)

import decision_gate
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


@pytest.fixture(autouse=True)
def _no_ambient_decision_gate(monkeypatch):
    """The `pii_guard` decision gate is off unless a test sets its variables — a developer who
    runs a local decision server would otherwise send every test digest to it."""
    monkeypatch.delenv("ONE_DECISION_MODEL", raising=False)
    monkeypatch.delenv("ONE_DECISION_URL", raising=False)


@pytest.fixture(autouse=True)
def _decision_gate_starts_on():
    """The gate switches itself off for the process after a transport failure — every test starts
    and ends with it on, so one test's dead server never silences the next."""
    decision_gate.reset()
    yield
    decision_gate.reset()
