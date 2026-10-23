"""`pc-models` is self-contained: copied alone, its server answers and its methodology matches pc-core's."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).parent.parent / "plugins"
PROTOCOL_VERSION = "2025-11-25"


def _exchange(server: Path, cwd: Path, home: Path, messages: list[dict]) -> dict:
    """Run the server on `server`, send `messages`, and return the response to the last one.

    Args:
        server: the server script to start.
        cwd: its working directory and project directory.
        home: its `HOME`.
        messages: the JSON-RPC messages to send, in order; the last carries an `id`.

    Returns:
        The JSON-RPC response whose `id` is the last message's.
    """
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(cwd),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("CLAUDE_PLUGIN_DATA", None)
    with subprocess.Popen(  # noqa: S603
        [sys.executable, str(server)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=cwd,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            for message in messages:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
            for line in proc.stdout:
                response = json.loads(line)
                if response.get("id") == messages[-1]["id"]:
                    return response
            raise AssertionError("the server closed before answering")
        finally:
            proc.kill()


def test_server_copied_alone_answers_pool_check_over_stdio(tmp_path):
    """The plugin, alone in an empty folder, starts and answers `pool_check`."""
    pytest.importorskip("mcp")
    alone = tmp_path / "empty" / "pc-models"
    shutil.copytree(
        PLUGINS / "pc-models", alone, ignore=shutil.ignore_patterns("__pycache__")
    )
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.pinky.json").write_text(
        json.dumps({"model_pool": []})
    )
    (tmp_path / "home").mkdir()

    answer = _exchange(
        alone / "mcp" / "pc_models_server.py",
        project,
        tmp_path / "home",
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pc-models-tests", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "pool_check", "arguments": {}},
            },
        ],
    )

    assert "error" not in answer
    assert answer["result"]["isError"] is False
    assert answer["result"]["content"][0]["text"]


def test_review_guidelines_copy_is_identical_to_pc_core_s():
    """The methodology `adversarial` reads is the one the `reviewer` agent reads."""
    original = PLUGINS / "pc-core" / "skills" / "review-guidelines" / "SKILL.md"
    copy = PLUGINS / "pc-models" / "skills" / "review-guidelines" / "SKILL.md"
    assert copy.read_bytes() == original.read_bytes()


def test_policy_lib_copy_is_identical_to_pc_core_s():
    """The vendored `policy_lib` stays the one the moteur distributes."""
    original = PLUGINS / "pc-core" / "hooks" / "policy_lib.py"
    copy = PLUGINS / "pc-models" / "hooks" / "pc_models_policy_lib.py"
    assert copy.read_bytes() == original.read_bytes()
