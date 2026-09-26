#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/kanban/scripts/pc_kanban_migrate_agent_log.py — run with:
python3 -m unittest discover tests
"""

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "skills"
        / "kanban"
        / "scripts"
    ),
)

import pc_kanban_migrate_agent_log as mal


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.kanban = self.root / "_KANBAN"
        self.kanban.mkdir()
        self.old = self.root / "_AGENT_LOG"
        self.new = self.kanban / "_AGENT_LOG"

    def test_moves_every_file_with_identical_content_and_leaves_relative_symlink(self):
        self.old.mkdir()
        (self.old / "2026-09-08_claude-code-Perrine_x.md").write_text("hello\n")
        (self.old / "2026-09-09_claude-code-Perrine_y.md").write_text("world\n")

        summary = mal.migrate(self.kanban)

        self.assertIn("migrated", summary)
        self.assertFalse(self.new.is_symlink())
        self.assertTrue(self.new.is_dir())
        self.assertEqual(
            (self.new / "2026-09-08_claude-code-Perrine_x.md").read_text(), "hello\n"
        )
        self.assertEqual(
            (self.new / "2026-09-09_claude-code-Perrine_y.md").read_text(), "world\n"
        )
        self.assertTrue(self.old.is_symlink())

    def test_symlink_is_relative_not_absolute(self):
        self.old.mkdir()
        (self.old / "note.md").write_text("x\n")
        mal.migrate(self.kanban)
        target = self.old.readlink()
        self.assertFalse(target.is_absolute())
        self.assertEqual(target.as_posix(), "_KANBAN/_AGENT_LOG")

    def test_write_through_old_path_lands_in_nested_dir(self):
        self.old.mkdir()
        (self.old / "note.md").write_text("x\n")
        mal.migrate(self.kanban)

        (self.old / "fresh.md").write_text("new entry\n")

        self.assertEqual((self.new / "fresh.md").read_text(), "new entry\n")

    def test_rerun_after_migration_is_a_noop(self):
        self.old.mkdir()
        (self.old / "note.md").write_text("x\n")
        mal.migrate(self.kanban)

        summary = mal.migrate(self.kanban)

        self.assertIn("already migrated", summary)
        self.assertEqual((self.new / "note.md").read_text(), "x\n")

    def test_fresh_board_with_no_sibling_is_a_noop(self):
        summary = mal.migrate(self.kanban)
        self.assertIn("nothing to migrate", summary)
        self.assertFalse(self.new.exists())
        self.assertFalse(self.old.exists())

    def test_both_real_directories_present_is_refused(self):
        self.old.mkdir()
        self.new.mkdir()
        with self.assertRaises(SystemExit) as cm:
            mal.migrate(self.kanban)
        self.assertIn("both", str(cm.exception))

    def test_symlink_pointing_elsewhere_is_refused(self):
        other = self.root / "elsewhere"
        other.mkdir()
        self.old.symlink_to(other, target_is_directory=True)
        with self.assertRaises(SystemExit) as cm:
            mal.migrate(self.kanban)
        self.assertIn("not the expected", str(cm.exception))

    def test_new_as_a_stray_file_is_refused_not_a_crash(self):
        self.old.mkdir()
        self.new.write_text("not a directory\n")
        with self.assertRaises(SystemExit) as cm:
            mal.migrate(self.kanban)
        self.assertIn("not a directory", str(cm.exception))

    def test_interrupted_migration_recovers_the_missing_symlink(self):
        """Simulates a process killed between `rename` and `symlink_to`: content already at
        `new`, `old` gone entirely. A rerun must recreate the symlink, not say "nothing to
        migrate" — that would silently fork content the next time an old-path writer runs."""
        self.new.mkdir()
        (self.new / "note.md").write_text("already moved\n")

        summary = mal.migrate(self.kanban)

        self.assertIn("recreated", summary)
        self.assertTrue(self.old.is_symlink())
        self.assertEqual((self.old / "note.md").read_text(), "already moved\n")


if __name__ == "__main__":
    unittest.main()
