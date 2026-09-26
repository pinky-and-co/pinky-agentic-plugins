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
