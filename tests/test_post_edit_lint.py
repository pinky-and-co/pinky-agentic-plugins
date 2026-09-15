#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/post_edit_lint.py — run with:
python3 -m unittest discover tests

Defines "done": a just-written file with a real violation produces a bounded, non-empty report; a
clean file, a file outside any git repo, or a non-Edit/Write tool produces None (silent, exit 0 —
this hook only informs, it never blocks); a missing/unreadable file never raises.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import post_edit_lint as pel

_REAL_CHECKERS_DIR = Path(__file__).parent.parent / "scripts" / "hooks"

ZWS = chr(0x200B)  # zero-width space — generated, not embedded literally


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    dst = tmp_path / "scripts" / "hooks"
    dst.mkdir(parents=True)
    for name in (
        "check_zws.py",
        "check_latex_notation.py",
        "check_forbidden_keywords.py",
        "check_typed_api.py",
    ):
        shutil.copy(_REAL_CHECKERS_DIR / name, dst / name)
    return tmp_path


def _edit_event(path: Path) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}


class TestDecide(unittest.TestCase):
    def test_reports_a_real_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            f = repo / f"note{ZWS}.md"
            f.write_text(f"hello{ZWS}world\n", encoding="utf-8")
            report = pel.decide(_edit_event(f))
            self.assertIsNotNone(report)
            self.assertIn("[ZWS]", report)

    def test_reports_raw_latex_notation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            f = repo / "note.md"
            f.write_text("A `SAINT` $\\rightarrow$ `ST`.\n", encoding="utf-8")
            report = pel.decide(_edit_event(f))
            self.assertIsNotNone(report)
            self.assertIn("[LATEX]", report)

    def test_clean_file_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            f = repo / "note.md"
            f.write_text("hello world\n", encoding="utf-8")
            self.assertIsNone(pel.decide(_edit_event(f)))

    def test_outside_any_repo_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "note.md"
            f.write_text(f"hello{ZWS}world\n", encoding="utf-8")
            self.assertIsNone(pel.decide(_edit_event(f)))

    def test_ignores_non_write_tool(self):
        self.assertIsNone(
            pel.decide({"tool_name": "Read", "tool_input": {"file_path": "x.py"}})
        )

    def test_missing_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            self.assertIsNone(pel.decide(_edit_event(repo / "nope.md")))

    def test_src_python_gets_typed_api_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            src = repo / "src"
            src.mkdir()
            f = src / "public.py"
            f.write_text(
                "def handler(x: tuple[int, int]) -> None:\n    ...\n", encoding="utf-8"
            )
            report = pel.decide(_edit_event(f))
            self.assertIsNotNone(report)
            self.assertIn("[TYPED-API]", report)

    def test_non_src_python_does_not_get_typed_api_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            f = repo / "public.py"
            f.write_text(
                "def handler(x: tuple[int, int]) -> None:\n    ...\n", encoding="utf-8"
            )
            self.assertIsNone(pel.decide(_edit_event(f)))

    def test_report_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            f = repo / "note.md"
            f.write_text(f"hello{ZWS}world\n", encoding="utf-8")
            with mock.patch.object(pel, "MAX_OUTPUT_LINES", 1):
                report = pel.decide(_edit_event(f))
            self.assertIsNotNone(report)
            self.assertEqual(
                len(report.splitlines()), 2
            )  # 1 kept + 1 truncation marker
            self.assertIn("truncated", report.splitlines()[-1])

    def test_run_checker_requests_utf8_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            f = repo / "note.md"
            f.write_text("hello world\n", encoding="utf-8")
            real_run = subprocess.run
            seen: dict = {}

            def _spy(*args, **kwargs):
                seen.update(kwargs)
                return real_run(*args, **kwargs)

            with mock.patch.object(subprocess, "run", _spy):
                pel.decide(_edit_event(f))
            self.assertEqual(seen.get("encoding"), "utf-8")

    def test_run_checker_survives_none_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            checker = repo / "scripts" / "hooks" / "check_zws.py"

            class _FakeResult:
                returncode = 1
                stdout = None

            with mock.patch.object(subprocess, "run", lambda *a, **k: _FakeResult()):
                result = pel._run_checker(checker, str(repo / "note.md"))
            self.assertIsNone(result)


class TestDisabledViaEnvVar(unittest.TestCase):
    def test_disabled_via_env_var(self):
        with mock.patch.dict("os.environ", {"PINKY_DISABLED_HOOKS": "post_edit_lint"}):
            self.assertTrue(pel._is_disabled())
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(pel._is_disabled())


if __name__ == "__main__":
    unittest.main()
