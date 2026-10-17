"""Tests for plugins/pc-read/skills/data-schema/scripts/data_schema.py."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("openpyxl")
pytest.importorskip("pyarrow")

import openpyxl  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

ROOT = Path(__file__).parent.parent / "plugins" / "pc-read"
sys.path.insert(0, str(ROOT / "skills" / "data-schema" / "scripts"))

import data_schema as ds  # noqa: E402

SHEETS = 8
ROWS = 5000


def make_workbook(path: Path, sheets: int = SHEETS, rows: int = ROWS) -> None:
    """Write a multi-sheet workbook: an int id, a text, a float and a date column."""
    book = openpyxl.Workbook()
    book.remove(book.active)
    for s in range(sheets):
        ws = book.create_sheet(f"feuille{s}")
        ws.append(["id", "label", "amount", "day"])
        for i in range(rows):
            ws.append(
                [i, f"item {i}", i * 1.5, __import__("datetime").date(2026, 1, 1)]
            )
    book.save(path)


def guard(root: Path, body: str) -> str:
    """A fake pc-privacy folder whose guard runs ``body``."""
    script = root / "hooks" / "pii_guard.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        f'# /// script\n# requires-python = ">=3.12"\n# ///\nimport sys, time\n{body}\n'
    )
    return str(root)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = mock.patch.dict(
            os.environ,
            {
                "XDG_CACHE_HOME": str(self.tmp / "cache"),
                "CLAUDE_CONFIG_DIR": str(self.tmp / "config"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.book = self.tmp / "data.xlsx"
        make_workbook(self.book)

    def run_main(self, path: Path, *extra: str) -> str:
        out = self.tmp / "out.md"
        code = ds.main([*extra, "--out", str(out), str(path)])
        self.assertEqual(code, 0)
        return out.read_text()

    def data_rows(self, text: str) -> int:
        return text.count("| item ")


class TestSchema(Base):
    def test_every_sheet_and_column_has_a_type(self):
        text = self.run_main(self.book)
        for s in range(SHEETS):
            self.assertIn(f"## Sheet: feuille{s}", text)
        self.assertEqual(text.count("| id | int |"), SHEETS)
        self.assertEqual(text.count("| label | str |"), SHEETS)
        self.assertEqual(text.count("| amount | float |"), SHEETS)
        self.assertEqual(text.count("| day | datetime |"), SHEETS)

    def test_csv_and_parquet(self):
        csv = self.tmp / "t.csv"
        csv.write_text("a;b\n1;x\n2;y\n")
        text = self.run_main(csv)
        self.assertIn("| a | int |", text)
        self.assertIn("| b | str |", text)
        pq_path = self.tmp / "t.parquet"
        pq.write_table(pa.table({"n": [1, 2], "s": ["u", "v"]}), pq_path)
        text = self.run_main(pq_path)
        self.assertIn("| n | int64 |", text)
        self.assertIn("| s | string |", text)

    def test_unsupported_suffix_fails(self):
        bad = self.tmp / "t.txt"
        bad.write_text("x")
        self.assertEqual(ds.main([str(bad)]), 2)


class TestSample(Base):
    def test_five_rows_from_five_thousand(self):
        text = self.run_main(self.book)
        # 5 rows in each of the 8 sheets, not 5 000
        self.assertEqual(self.data_rows(text), 5 * SHEETS)
        one = ds.read_xlsx(self.book)[0]
        self.assertEqual(len(one[3]), 5)

    def test_single_sheet_of_5000_rows_gives_5(self):
        book = self.tmp / "one.xlsx"
        make_workbook(book, sheets=1)
        self.assertEqual(self.data_rows(self.run_main(book)), 5)


class TestPrivacy(Base):
    def test_flagged_gives_schema_only(self):
        root = guard(self.tmp / "p", "print('{\"flagged\": true}')")
        text = self.run_main(self.book, "--privacy-root", root)
        self.assertEqual(self.data_rows(text), 0)
        self.assertIn("| id | int |", text)
        self.assertIn("Rows withheld", text)

    def test_guard_failure_gives_schema_only(self):
        root = guard(self.tmp / "p", "sys.exit(3)")
        text = self.run_main(self.book, "--privacy-root", root)
        self.assertEqual(self.data_rows(text), 0)
        self.assertIn("| id | int |", text)

    def test_guard_timeout_gives_schema_only(self):
        root = guard(self.tmp / "p", "time.sleep(30)")
        with mock.patch.object(ds, "GUARD_TIMEOUT_S", 1):
            text = self.run_main(self.book, "--privacy-root", root)
        self.assertEqual(self.data_rows(text), 0)
        self.assertIn("| id | int |", text)

    def test_unparseable_guard_output_gives_schema_only(self):
        root = guard(self.tmp / "p", "print('not json')")
        self.assertEqual(
            self.data_rows(self.run_main(self.book, "--privacy-root", root)), 0
        )

    def test_clean_verdict_gives_sample(self):
        root = guard(self.tmp / "p", "print('{\"flagged\": false}')")
        self.assertEqual(
            self.data_rows(self.run_main(self.book, "--privacy-root", root)), 5 * SHEETS
        )

    def test_no_pc_privacy_gives_sample(self):
        """No folder given and no installed_plugins.json under the empty config dir."""
        self.assertEqual(self.data_rows(self.run_main(self.book)), 5 * SHEETS)

    def test_installed_pc_privacy_is_found_and_obeyed(self):
        root = guard(self.tmp / "installed", "print('{\"flagged\": true}')")
        plugins = self.tmp / "config" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"pc-privacy@m": [{"installPath": root}]}})
        )
        self.assertEqual(self.data_rows(self.run_main(self.book)), 0)

    def _register(self, source: dict, install: str) -> None:
        plugins = self.tmp / "config" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"pc-privacy@m": [{"installPath": install}]}})
        )
        (plugins / "known_marketplaces.json").write_text(
            json.dumps({"m": {"source": source}})
        )

    def test_directory_marketplace_uses_the_working_tree(self):
        stale = guard(self.tmp / "cache-copy", "print('{\"flagged\": false}')")
        tree = self.tmp / "tree"
        guard(tree / "plugins" / "pc-privacy", "print('{\"flagged\": true}')")
        self._register({"source": "directory", "path": str(tree)}, stale)
        self.assertEqual(ds.privacy_root(None), str(tree / "plugins" / "pc-privacy"))
        self.assertEqual(self.data_rows(self.run_main(self.book)), 0)

    def test_github_marketplace_uses_install_path(self):
        installed = guard(self.tmp / "cache-copy", "print('{\"flagged\": true}')")
        self._register({"source": "github", "repo": "o/r"}, installed)
        self.assertEqual(ds.privacy_root(None), installed)
        self.assertEqual(self.data_rows(self.run_main(self.book)), 0)


class TestSize(Base):
    @unittest.skipUnless(shutil.which("markitdown"), "markitdown not installed")
    def test_mode_output_is_smaller_than_markitdown(self):
        mode = len(self.run_main(self.book).encode())
        md = len(
            subprocess.run(
                ["markitdown", str(self.book)], capture_output=True, check=True
            ).stdout
        )
        print(f"size: mode={mode} bytes, markitdown={md} bytes", file=sys.stderr)
        self.assertLess(mode, md)


if __name__ == "__main__":
    unittest.main()
