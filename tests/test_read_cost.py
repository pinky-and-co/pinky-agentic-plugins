#!/usr/bin/env python3
"""Tests for plugins/pc-read/hooks/read_cost.py — run with:
python3 -m unittest discover tests
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-read" / "hooks"))

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
            "`document-to-md` skill (`pc-read`)",
            decision["hookSpecificOutput"]["permissionDecisionReason"],
        )


class TestVerdict(unittest.TestCase):
    PDF = {"tool_name": "Read", "tool_input": {"file_path": "/docs/report.pdf"}}

    def test_costly_file_is_flagged_with_its_path(self):
        with mock.patch.object(rc.shutil, "which", return_value="/bin/markitdown"):
            self.assertEqual(
                rc.verdict(self.PDF),
                {
                    "flagged": True,
                    "path": "/docs/report.pdf",
                    "markitdown": True,
                    "data": False,
                },
            )

    def test_only_a_workbook_offers_the_data_mode(self):
        book = {"tool_name": "Read", "tool_input": {"file_path": "/docs/b.xlsx"}}
        self.assertIs(rc.verdict(book)["data"], True)

    def test_missing_converter_is_reported(self):
        with mock.patch.object(rc.shutil, "which", return_value=None):
            self.assertIs(rc.verdict(self.PDF)["markitdown"], False)

    def test_ordinary_file_is_not_flagged(self):
        self.assertEqual(
            rc.verdict({"tool_name": "Read", "tool_input": {"file_path": "app.py"}}),
            {"flagged": False},
        )


class TestConversionTarget(unittest.TestCase):
    def test_same_name_in_two_folders_gives_two_paths(self):
        first = rc.conversion_target("a/x.pdf")
        second = rc.conversion_target("b/x.pdf")
        self.addCleanup(shutil.rmtree, Path(first).parent, True)
        self.addCleanup(shutil.rmtree, Path(second).parent, True)
        self.assertNotEqual(first, second)

    def test_keeps_the_name_and_swaps_the_suffix(self):
        target = rc.conversion_target("/docs/report.PDF")
        self.addCleanup(shutil.rmtree, Path(target).parent, True)
        self.assertEqual(Path(target).name, "report.md")

    def test_folder_exists_and_is_private(self):
        target = rc.conversion_target("report.pdf")
        folder = Path(target).parent
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertTrue(folder.is_dir())
        self.assertFalse(Path(target).exists())
        if sys.platform != "win32":
            self.assertEqual(folder.stat().st_mode & 0o077, 0)


class TestDiscardTarget(unittest.TestCase):
    def test_removes_the_folder_conversion_target_made(self):
        target = rc.conversion_target("report.pdf")
        self.assertTrue(rc.discard_target(target))
        self.assertFalse(Path(target).parent.exists())

    def test_refuses_a_folder_it_did_not_make(self):
        with tempfile.TemporaryDirectory(prefix="other-") as folder:
            self.assertFalse(rc.discard_target(str(Path(folder) / "x.md")))
            self.assertTrue(Path(folder).is_dir())

    def test_refuses_a_pc_read_folder_outside_the_temp_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            folder = Path(parent) / "pc-read-abc"
            folder.mkdir()
            self.assertFalse(rc.discard_target(str(folder / "x.md")))
            self.assertTrue(folder.is_dir())

    def test_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as real:
            link = Path(tempfile.gettempdir()) / f"pc-read-{uuid.uuid4().hex}"
            link.symlink_to(real)
            try:
                self.assertFalse(rc.discard_target(str(link / "x.md")))
                self.assertTrue(Path(real).is_dir())
            finally:
                link.unlink()


class TestMainEndToEnd(unittest.TestCase):
    """Real subprocess calls — same invocation shape Claude Code uses (JSON payload on stdin)."""

    SCRIPT = str(
        Path(__file__).parent.parent / "plugins" / "pc-read" / "hooks" / "read_cost.py"
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

    def test_verdict_mode_prints_json(self):
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--verdict"],
            input=json.dumps(
                {"tool_name": "Read", "tool_input": {"file_path": "book.xlsx"}}
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        output = json.loads(result.stdout)
        self.assertEqual(
            (result.returncode, output["flagged"], output["path"]),
            (0, True, "book.xlsx"),
        )

    def test_target_mode_prints_distinct_paths_for_same_name(self):
        paths = []
        for source in ("a/x.pdf", "b/x.pdf"):
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "--target", source],
                capture_output=True,
                text=True,
                check=False,
            )
            path = json.loads(result.stdout)["markdown"]
            self.addCleanup(shutil.rmtree, Path(path).parent, True)
            paths.append(path)
        self.assertEqual(len(set(paths)), 2)


if __name__ == "__main__":
    unittest.main()
