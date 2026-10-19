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
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_cmux(monkeypatch):
    """A session run from a cmux pane carries `CMUX_SURFACE_ID`, which makes `run_pool(watch=True)`
    open a split once per run — every test starts without it, a test that wants the split sets it
    itself."""
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_decision_gate(monkeypatch, tmp_path_factory):
    """The `pii_guard` decision gate is off unless a test sets its variables — a developer who
    runs a local decision server would otherwise send every test digest to it. The server address
    also comes from the `mlx` provider of the developer's `models.json`, so `PI_CODING_AGENT_DIR`
    points at an empty folder."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path_factory.mktemp("pi-agent")))
    monkeypatch.delenv("ONE_DECISION_MODEL", raising=False)
    monkeypatch.delenv("LOCAL_INFERENCE_BASE_URL", raising=False)


@pytest.fixture(autouse=True)
def _decision_gate_starts_on():
    """The gate switches itself off for the process after a transport failure — every test starts
    and ends with it on, so one test's dead server never silences the next."""
    decision_gate.reset()
    yield
    decision_gate.reset()
