#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/read_cost.py — run with:
python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import read_cost as rc


class TestTargetSuffix(unittest.TestCase):
    def test_lowercases_suffix(self):
        self.assertEqual(rc.target_suffix({"file_path": "report.PDF"}), ".pdf")

    def test_absent_file_path_returns_empty(self):
        self.assertEqual(rc.target_suffix({}), "")


class TestDecide(unittest.TestCase):
    def test_asks_on_pdf(self):
        decision = rc.decide(
            {"tool_name": "Read", "tool_input": {"file_path": "report.pdf"}}
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_asks_on_html(self):
        decision = rc.decide(
            {"tool_name": "Read", "tool_input": {"file_path": "page.html"}}
        )
        self.assertIsNotNone(decision)

    def test_asks_on_xlsx_docx_pptx(self):
        for ext in (".xlsx", ".docx", ".pptx"):
            with self.subTest(ext=ext):
                decision = rc.decide(
                    {"tool_name": "Read", "tool_input": {"file_path": f"f{ext}"}}
                )
                self.assertIsNotNone(decision)

    def test_silent_on_python_file(self):
        self.assertIsNone(
            rc.decide({"tool_name": "Read", "tool_input": {"file_path": "app.py"}})
        )

    def test_silent_on_non_read_tool(self):
        self.assertIsNone(
            rc.decide({"tool_name": "Write", "tool_input": {"file_path": "f.pdf"}})
        )

    def test_message_names_document_to_md(self):
        decision = rc.decide(
            {"tool_name": "Read", "tool_input": {"file_path": "report.pdf"}}
        )
        self.assertIn(
            "document-to-md",
            decision["hookSpecificOutput"]["permissionDecisionReason"],
        )


class TestMainEndToEnd(unittest.TestCase):
    """Real subprocess calls — same invocation shape Claude Code uses (JSON payload on stdin)."""

    SCRIPT = str(
        Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks" / "read_cost.py"
    )

    def _run(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_asks_over_stdin(self):
        result = self._run(
            {"tool_name": "Read", "tool_input": {"file_path": "report.pdf"}}
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_silent_over_stdin_for_ordinary_file(self):
        result = self._run({"tool_name": "Read", "tool_input": {"file_path": "app.py"}})
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
