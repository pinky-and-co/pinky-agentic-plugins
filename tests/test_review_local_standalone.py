#!/usr/bin/env python3
"""Tests for plugins/pc-models/evals/review-local/standalone.py — run with:
uv run pytest tests/test_review_local_standalone.py -q
"""

import importlib.util
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

_STANDALONE = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-models"
    / "evals"
    / "review-local"
    / "standalone.py"
)

_spec = importlib.util.spec_from_file_location("review_local_standalone", _STANDALONE)
assert _spec is not None and _spec.loader is not None
standalone = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = standalone
_spec.loader.exec_module(standalone)


class TestAskPiCommand(unittest.TestCase):
    """`ask_pi` must pass `read,grep` explicitly — never `--no-tools`, which turns a diff
    review into tool-call text."""

    def test_the_built_command_passes_read_grep_explicitly(self):
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with unittest.mock.patch("subprocess.run", return_value=done) as run:
            answer = standalone.ask_pi("prompt", "mlx/org/model", None, 60)
        self.assertEqual(answer, "ok")
        run.assert_called_once_with(
            [
                "pi",
                "-p",
                "--model",
                "mlx/org/model",
                "-t",
                "read,grep",
                "--",
                "prompt",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_the_thinking_flag_keeps_its_place_before_the_prompt(self):
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with unittest.mock.patch("subprocess.run", return_value=done) as run:
            standalone.ask_pi("prompt", "m", "off", 60)
        self.assertEqual(
            run.call_args[0][0],
            [
                "pi",
                "-p",
                "--model",
                "m",
                "-t",
                "read,grep",
                "--thinking",
                "off",
                "--",
                "prompt",
            ],
        )


if __name__ == "__main__":
    unittest.main()
