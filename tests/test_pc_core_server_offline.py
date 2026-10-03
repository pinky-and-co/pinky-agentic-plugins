#!/usr/bin/env python3
"""Tests for `pc_core_server.py::offline()` — the production zero-egress entry point that
`tests/test_model_offline.py` exercises indirectly via `model_offline.main()`, never directly."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pytest


def _import_server():
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp")
    )
    import pc_core_server

    return pc_core_server


class TestOfflineToolZeroEgress(unittest.TestCase):
    def setUp(self):
        # La réponse va dans un dossier propre au test, pas dans /tmp où elle resterait après le run.
        server = _import_server()
        self.out_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            unittest.mock.patch.object(
                server.model_offline,
                "default_out",
                lambda: self.out_dir / "answer.md",
            )
        )

    @unittest.mock.patch("subprocess.run")
    def test_success_returns_status_line_never_the_body(self, mock_run):
        server = _import_server()
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="SENSITIVE ANSWER BODY", stderr="", returncode=0
        )
        out = server.offline("do it")
        self.assertNotIn("SENSITIVE ANSWER BODY", out)
        self.assertTrue(out.startswith("[offline]"))
        self.assertIn(str(self.out_dir / "answer.md"), out)

    @unittest.mock.patch("subprocess.run")
    def test_pi_failure_never_leaks_stdout_stderr(self, mock_run):
        server = _import_server()
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "pi", output="ECHOED SENSITIVE PROMPT", stderr="ALSO SENSITIVE"
        )
        out = server.offline("do it")
        self.assertNotIn("ECHOED SENSITIVE PROMPT", out)
        self.assertNotIn("ALSO SENSITIVE", out)
        self.assertIn("BLOCKED", out)


if __name__ == "__main__":
    unittest.main()
