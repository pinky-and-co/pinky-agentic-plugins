#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/init/scripts/pc_kanban_init.py — run with:
python3 -m unittest discover tests
"""

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
import sys

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "skills"
        / "init"
        / "scripts"
    ),
)

import pc_kanban_init as pki


class InitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.settings = Path(self._tmp.name) / "settings.pinky.json"
        self.claude_settings = Path(self._tmp.name) / "settings.json"
        self._patch = unittest.mock.patch.multiple(
            pki,
            SETTINGS_PINKY_PATH=self.settings,
            CLAUDE_SETTINGS_PATH=self.claude_settings,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _settings_data(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def _claude_settings_data(self) -> dict:
        return json.loads(self.claude_settings.read_text(encoding="utf-8"))


class TestRenderIndex(unittest.TestCase):
    def test_empty_board_is_header_only(self):
        self.assertEqual(
            pki.render_index([]),
            "---\n"
            "title: KANBAN\n"
            "---\n"
            "# _KANBAN — board de chantiers\n"
            "\n"
            "## Board\n"
            "\n"
            f"{pki.LEGEND}\n"
            "\n"
            "| # | Chantier (objectif) | Statut |\n"
            "|---|---------------------|--------|\n",
        )

    def test_recovered_chantiers_become_dormant_rows(self):
        self.assertEqual(
            pki.render_index([("0025", "plugins-claude"), ("0099", "demo")]),
            "---\n"
            "title: KANBAN\n"
            "---\n"
            "# _KANBAN — board de chantiers\n"
            "\n"
            "## Board\n"
            "\n"
            f"{pki.LEGEND}\n"
            "\n"
            "| # | Chantier (objectif) | Statut |\n"
            "|---|---------------------|--------|\n"
            "| 0025 | plugins-claude | ⚪ |\n"
            "| 0099 | demo | ⚪ |\n",
        )


class TestExistingChantiers(unittest.TestCase):
    def test_scans_chantier_dirs_ignoring_non_matching_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            kanban = Path(tmp)
            (kanban / "CHANTIER0099-demo").mkdir()
            (kanban / "CHANTIER0001-alpha").mkdir()
            (kanban / "INDEX.md").write_text("x\n")
            (kanban / "CHANTIERnope").mkdir()
            self.assertEqual(
                pki.existing_chantiers(kanban),
                [("0001", "alpha"), ("0099", "demo")],
            )

    def test_no_chantiers_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pki.existing_chantiers(Path(tmp)), [])


class TestWriteKanbanBoards(InitTestCase):
    def test_creates_the_settings_file_and_key_on_a_virgin_root(self):
        summary = pki.write_kanban_boards(self.root)

        self.assertTrue(self.settings.is_file())
        data = self._settings_data()
        self.assertEqual(data["kanban_boards"], {str(self.root.resolve()): []})
        self.assertNotIn("model_pool", data)
        self.assertNotIn("keys", data)
        self.assertIn(str(self.root.resolve()), summary)

    def test_relative_vs_absolute_root_resolve_to_one_key(self):
        cwd = os.getcwd()
        try:
            os.chdir(self.root.parent)
            pki.write_kanban_boards(Path(self.root.name))
            pki.write_kanban_boards(self.root.resolve())
        finally:
            os.chdir(cwd)

        data = self._settings_data()
        self.assertEqual(list(data["kanban_boards"].keys()), [str(self.root.resolve())])

    def test_model_pool_and_keys_are_never_touched(self):
        self.settings.write_text(
            json.dumps({"model_pool": [{"name": "x"}], "keys": [{"env": "Y"}]}),
            encoding="utf-8",
        )

        pki.write_kanban_boards(self.root)

        data = self._settings_data()
        self.assertEqual(data["model_pool"], [{"name": "x"}])
        self.assertEqual(data["keys"], [{"env": "Y"}])

    def test_a_hand_declared_entry_is_never_touched_by_a_rerun(self):
        """Declaring an extra board for an already-initialized root is a hand edit — init reads
        nothing else to populate or alter it, on the first run or any later one."""
        other = Path(self._tmp.name) / "hand-declared"
        other.mkdir()
        self.settings.write_text(
            json.dumps({"kanban_boards": {str(self.root.resolve()): [str(other)]}}),
            encoding="utf-8",
        )

        summary = pki.write_kanban_boards(self.root)

        data = self._settings_data()
        self.assertEqual(data["kanban_boards"][str(self.root.resolve())], [str(other)])
        self.assertIn("unchanged", summary)

    def test_rerun_with_an_existing_key_does_not_touch_the_file(self):
        pki.write_kanban_boards(self.root)
        before = self.settings.read_text(encoding="utf-8")
        before_mtime = self.settings.stat().st_mtime_ns

        pki.write_kanban_boards(self.root)

        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertEqual(self.settings.stat().st_mtime_ns, before_mtime)

    def test_malformed_settings_file_is_never_overwritten(self):
        self.settings.write_text("not json", encoding="utf-8")

        with self.assertRaises(SystemExit):
            pki.write_kanban_boards(self.root)

        self.assertEqual(self.settings.read_text(encoding="utf-8"), "not json")

    def test_settings_file_whose_top_level_is_a_list_is_never_overwritten(self):
        self.settings.write_text("[1, 2, 3]", encoding="utf-8")

        with self.assertRaises(SystemExit):
            pki.write_kanban_boards(self.root)

        self.assertEqual(self.settings.read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_write_leaves_no_tmp_file_behind(self):
        pki.write_kanban_boards(self.root)
        self.assertEqual(
            list(self.settings.parent.glob(f"{self.settings.name}.tmp")), []
        )


class TestWriteAdditionalDirectories(InitTestCase):
    def test_grants_the_root_kanban_on_a_virgin_settings_json(self):
        summary = pki.write_additional_directories(self.root)

        data = self._claude_settings_data()
        self.assertEqual(
            data["permissions"]["additionalDirectories"],
            [str(self.root.resolve() / "_KANBAN")],
        )
        self.assertIn(str(self.root.resolve() / "_KANBAN"), summary)

    def test_grants_every_board_declared_in_kanban_boards_too(self):
        other = Path(self._tmp.name) / "other-board"
        other.mkdir()
        self.settings.write_text(
            json.dumps({"kanban_boards": {str(self.root.resolve()): [str(other)]}}),
            encoding="utf-8",
        )

        pki.write_additional_directories(self.root)

        data = self._claude_settings_data()
        self.assertEqual(
            set(data["permissions"]["additionalDirectories"]),
            {str(self.root.resolve() / "_KANBAN"), str(other.resolve() / "_KANBAN")},
        )

    def test_an_existing_unrelated_entry_is_never_dropped(self):
        self.claude_settings.write_text(
            json.dumps(
                {"permissions": {"additionalDirectories": ["/Users/x/.claude"]}}
            ),
            encoding="utf-8",
        )

        pki.write_additional_directories(self.root)

        data = self._claude_settings_data()
        self.assertIn("/Users/x/.claude", data["permissions"]["additionalDirectories"])
        self.assertIn(
            str(self.root.resolve() / "_KANBAN"),
            data["permissions"]["additionalDirectories"],
        )

    def test_hooks_and_enabled_plugins_are_never_touched(self):
        self.claude_settings.write_text(
            json.dumps({"hooks": {"Stop": []}, "enabledPlugins": {"x": True}}),
            encoding="utf-8",
        )

        pki.write_additional_directories(self.root)

        data = self._claude_settings_data()
        self.assertEqual(data["hooks"], {"Stop": []})
        self.assertEqual(data["enabledPlugins"], {"x": True})

    def test_rerun_with_nothing_new_does_not_touch_the_file(self):
        pki.write_additional_directories(self.root)
        before = self.claude_settings.read_text(encoding="utf-8")
        before_mtime = self.claude_settings.stat().st_mtime_ns

        pki.write_additional_directories(self.root)

        self.assertEqual(self.claude_settings.read_text(encoding="utf-8"), before)
        self.assertEqual(self.claude_settings.stat().st_mtime_ns, before_mtime)

    def test_malformed_claude_settings_is_never_overwritten(self):
        self.claude_settings.write_text("not json", encoding="utf-8")

        with self.assertRaises(SystemExit):
            pki.write_additional_directories(self.root)

        self.assertEqual(self.claude_settings.read_text(encoding="utf-8"), "not json")


class TestInit(InitTestCase):
    def test_creates_kanban_and_nested_agent_log_on_a_virgin_root(self):
        summary = pki.init(self.root)

        self.assertTrue((self.root / "_KANBAN").is_dir())
        self.assertTrue((self.root / "_KANBAN" / "_AGENT_LOG").is_dir())
        self.assertTrue((self.root / "_KANBAN" / "INDEX.md").is_file())
        self.assertIn(str(self.root / "_KANBAN"), summary)

    def test_grants_additional_directories_on_a_virgin_root(self):
        pki.init(self.root)

        data = self._claude_settings_data()
        self.assertIn(
            str(self.root.resolve() / "_KANBAN"),
            data["permissions"]["additionalDirectories"],
        )

    def test_never_scaffolds_a_first_chantier(self):
        pki.init(self.root)
        self.assertEqual(list((self.root / "_KANBAN").glob("CHANTIER*")), [])

    def test_index_recovers_chantiers_already_on_disk(self):
        (self.root / "_KANBAN" / "CHANTIER0025-plugins-claude").mkdir(parents=True)

        pki.init(self.root)

        text = (self.root / "_KANBAN" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("| 0025 | plugins-claude | ⚪ |", text)

    def test_index_already_present_is_never_overwritten(self):
        kanban = self.root / "_KANBAN"
        kanban.mkdir()
        (kanban / "INDEX.md").write_text("hand-authored\n", encoding="utf-8")

        pki.init(self.root)

        self.assertEqual(
            (kanban / "INDEX.md").read_text(encoding="utf-8"), "hand-authored\n"
        )

    def test_pre_existing_sibling_agent_log_is_folded_in_not_shadowed(self):
        kanban = self.root / "_KANBAN"
        kanban.mkdir()
        sibling = self.root / "_AGENT_LOG"
        sibling.mkdir()
        (sibling / "entry.md").write_text("hello\n", encoding="utf-8")

        pki.init(self.root)

        nested = kanban / "_AGENT_LOG"
        self.assertTrue(nested.is_dir())
        self.assertEqual((nested / "entry.md").read_text(encoding="utf-8"), "hello\n")
        self.assertFalse(sibling.exists())

    def test_rerun_on_an_already_scaffolded_root_changes_nothing(self):
        pki.init(self.root)
        index = self.root / "_KANBAN" / "INDEX.md"
        index_before = index.read_text(encoding="utf-8")
        index_mtime_before = index.stat().st_mtime_ns
        settings_before = self.settings.read_text(encoding="utf-8")
        claude_settings_before = self.claude_settings.read_text(encoding="utf-8")

        pki.init(self.root)

        self.assertEqual(index.read_text(encoding="utf-8"), index_before)
        self.assertEqual(index.stat().st_mtime_ns, index_mtime_before)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), settings_before)
        self.assertEqual(
            self.claude_settings.read_text(encoding="utf-8"), claude_settings_before
        )

    def test_refuses_to_nest_a_second_board_under_an_already_boarded_ancestor(self):
        pki.init(self.root)
        nested_root = self.root / "sub" / "deeper"
        nested_root.mkdir(parents=True)

        with self.assertRaises(SystemExit):
            pki.init(nested_root)

        self.assertEqual(list((nested_root).glob("_KANBAN")), [])

    def test_migration_failure_is_never_papered_over_with_a_blank_agent_log(self):
        """Both the sibling and the nested `_AGENT_LOG/` already real (the migration script's own
        refusal case) must stop `init` cold — never silently create an empty nested dir next to
        content the migration couldn't fold in."""
        kanban = self.root / "_KANBAN"
        kanban.mkdir()
        sibling = self.root / "_AGENT_LOG"
        sibling.mkdir()
        (sibling / "sibling-entry.md").write_text("sibling\n", encoding="utf-8")
        nested = kanban / "_AGENT_LOG"
        nested.mkdir()
        (nested / "nested-entry.md").write_text("nested\n", encoding="utf-8")

        with self.assertRaises(SystemExit):
            pki.init(self.root)

        self.assertFalse((kanban / "INDEX.md").exists())
        self.assertEqual(
            (sibling / "sibling-entry.md").read_text(encoding="utf-8"), "sibling\n"
        )
        self.assertEqual(
            (nested / "nested-entry.md").read_text(encoding="utf-8"), "nested\n"
        )


if __name__ == "__main__":
    unittest.main()
