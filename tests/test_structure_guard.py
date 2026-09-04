#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/structure_guard.py — run with:
python3 -m unittest discover tests

Defines "done": `decide()` is a pure interpreter over a repo-declared `file_placement` rule list
(FR-0023-51..54) — no rule is Python-hardcoded (NFR-0023-24). A `Write` violating a matched rule
returns permissionDecision "deny"; a compliant Write, a non-Write tool, an unmatched root, or a
repo without `file_placement` in its resolved settings.pinky.json returns no decision
(NFR-0023-23); a repo can locally override an ancestor's `file_placement` (same closest-wins
walk-up as any other settings.pinky.json key); the deny reason is actionable (IMP-0023-29).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import structure_guard as sg  # noqa: E402

# The real pinky-agentic-plugins rule set — a fixture here, not a module constant in
# structure_guard.py, is the whole point of NFR-0023-24: this is one repo's data, not the code's.
PLUGIN_RULES = [
    {
        "root": "plugins",
        "root_dirs": ["hooks", "skills", "mcp", "scripts", "agents", ".claude-plugin"],
        "root_files": ["README.md", ".mcp.json", "settings.json"],
        "forbidden_dirs": ["commands"],
        "reserved_dirs": {"scripts": "pc-statusline"},
        "leaf_subdirs": {
            "skills": {
                "required_file": "SKILL.md",
                "subdirs": [
                    "scripts",
                    "references",
                    "examples",
                    "templates",
                    "assets",
                    "evals",
                ],
            }
        },
    }
]


class TestDecideBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name) / "repo"
        self.repo_root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _event(self, rel_path: str) -> dict:
        return {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.repo_root / "plugins" / rel_path)},
        }

    def decide(self, rel_path: str, rules=PLUGIN_RULES):
        return sg.decide(self._event(rel_path), self.repo_root, rules)


class TestDecidePluginRoot(TestDecideBase):
    def test_denies_stray_root_dir(self):
        decision = self.decide("pc-core/vendor/foo.py")
        self.assertIsNotNone(decision)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_denies_stray_root_file(self):
        self.assertIsNotNone(self.decide("pc-core/NOTES.md"))

    def test_allows_known_root_dir(self):
        self.assertIsNone(self.decide("pc-core/hooks/foo.py"))

    def test_allows_known_root_file(self):
        self.assertIsNone(self.decide("pc-core/README.md"))
        self.assertIsNone(self.decide("pc-core/.mcp.json"))

    def test_allows_agents_dir(self):
        # agents/ was missing from the first draft of this rule — regression guard.
        self.assertIsNone(self.decide("pc-core/agents/reviewer.md"))


class TestDecideCommands(TestDecideBase):
    def test_denies_commands_anywhere(self):
        decision = self.decide("pc-kanban/skills/grill/commands/foo.md")
        self.assertIsNotNone(decision)
        self.assertIn(
            "commands", decision["hookSpecificOutput"]["permissionDecisionReason"]
        )

    def test_denies_commands_at_plugin_root(self):
        self.assertIsNotNone(self.decide("pc-core/commands/foo.md"))


class TestDecideScriptsReserved(TestDecideBase):
    def test_denies_root_scripts_outside_statusline(self):
        self.assertIsNotNone(self.decide("pc-core/scripts/foo.py"))

    def test_allows_root_scripts_for_statusline(self):
        self.assertIsNone(self.decide("pc-statusline/scripts/statusline.py"))


class TestDecideSkillSubdirs(TestDecideBase):
    def test_denies_loose_file_in_skill_dir(self):
        self.assertIsNotNone(self.decide("pc-kanban/skills/grill/notes.md"))

    def test_allows_skill_md(self):
        self.assertIsNone(self.decide("pc-kanban/skills/grill/SKILL.md"))

    def test_allows_known_skill_subdir(self):
        for sub in (
            "scripts",
            "references",
            "examples",
            "templates",
            "assets",
            "evals",
        ):
            with self.subTest(sub=sub):
                self.assertIsNone(self.decide(f"pc-kanban/skills/grill/{sub}/foo.py"))

    def test_denies_unknown_skill_subdir(self):
        self.assertIsNotNone(self.decide("pc-kanban/skills/grill/weird/foo.py"))

    def test_denies_flat_file_instead_of_skill_directory(self):
        # The headline case FR-0023-54 exists for: a flat `skills/<name>.md` file instead of a
        # `skills/<name>/SKILL.md` directory. Regression guard for the v1 gap (len(rest) >= 3
        # skipped this 2-segment shape entirely).
        decision = self.decide("pc-kanban/skills/newskill.md")
        self.assertIsNotNone(decision)
        reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
        # "directory" alone also matches the root_dirs/forbidden_dirs branches — pin the specific
        # phrase this branch alone produces, not a word shared by every deny message.
        self.assertIn("must be a directory", reason)


class TestScopeAndSafety(TestDecideBase):
    def test_ignores_edit_tool(self):
        event = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(self.repo_root / "plugins" / "pc-core" / "x.py")
            },
        }
        self.assertIsNone(sg.decide(event, self.repo_root, PLUGIN_RULES))

    def test_ignores_write_outside_every_root(self):
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.repo_root / "tests" / "foo.py")},
        }
        self.assertIsNone(sg.decide(event, self.repo_root, PLUGIN_RULES))

    def test_empty_rule_list_never_matches(self):
        self.assertIsNone(self.decide("pc-core/vendor/foo.py", rules=[]))

    def test_deny_reason_is_actionable(self):
        decision = self.decide("pc-core/vendor/foo.py")
        reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertTrue(reason)
        self.assertIn(
            "hooks", reason
        )  # names an allowed directory, not just "rejected"


class TestNRoots(TestDecideBase):
    """NFR-0023-24: a repo declares as many independent conventions as it wants, each scoped to
    its own `root` — no cross-talk between them, no code change to add a second one."""

    RULES = [
        {"root": "plugins", "root_dirs": ["hooks"]},
        {"root": "src", "root_dirs": ["core"], "forbidden_dirs": ["legacy"]},
    ]

    def test_first_root_applies_its_own_rule(self):
        # A directory-shaped Write (not a bare root file) exercises root_dirs — root_files is
        # absent on this rule, so it never constrains a single file directly at the root.
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "plugins" / "x" / "vendor" / "y.py")
            },
        }
        self.assertIsNotNone(sg.decide(event, self.repo_root, self.RULES))

    def test_second_root_applies_its_own_rule_independently(self):
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "src" / "y" / "legacy" / "z.py")
            },
        }
        self.assertIsNotNone(sg.decide(event, self.repo_root, self.RULES))

    def test_second_root_is_not_constrained_by_first_roots_dirs(self):
        # "core" is not in plugins' root_dirs, but plugins' rule never applies here.
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "src" / "y" / "core" / "z.py")
            },
        }
        self.assertIsNone(sg.decide(event, self.repo_root, self.RULES))

    def test_degenerate_match_on_a_duplicate_root_does_not_crash_or_deny(self):
        # A Write directly at `<root>/<unit>` with nothing under it (e.g. a duplicated/redundant
        # `root` entry) is degenerate — v1 bug returned None from `_matching_rule` on that case
        # via `return`, which is harmless when it's the only rule but would have skipped the rest
        # of the list for a real match on a different root. `continue` (not `return`) is the fix.
        # Under the documented "roots must be disjoint" constraint, two prefixes of the same path
        # are always ordered by inclusion, so at most one rule can ever match a given file — this
        # only pins the safety property (no crash, no false deny), it cannot observe `continue`
        # reaching a second, genuinely different match (that would require nested roots, out of
        # scope by contract).
        rules = [{"root": "plugins"}, {"root": "plugins"}]
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "plugins" / "onlyfile.py")
            },
        }
        self.assertIsNone(sg.decide(event, self.repo_root, rules))


class TestMalformedRuleData(TestDecideBase):
    """A rule author's typo degrades that key to "no constraint" + a stderr warning — it must
    never raise, and never be silently reinterpreted into different, wrong behavior."""

    def test_non_list_forbidden_dirs_is_ignored_not_misread(self):
        # A bare string would otherwise silently become `set("commands")` — matching single
        # characters as directory names. Must be ignored instead.
        rules = [{"root": "plugins", "forbidden_dirs": "commands"}]
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "plugins" / "x" / "c" / "y.py")
            },
        }
        self.assertIsNone(sg.decide(event, self.repo_root, rules))

    def test_non_dict_reserved_dirs_does_not_raise(self):
        rules = [
            {
                "root": "plugins",
                "root_dirs": ["scripts"],
                "reserved_dirs": ["pc-statusline"],
            }
        ]
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "plugins" / "x" / "scripts" / "y.py")
            },
        }
        self.assertIsNone(sg.decide(event, self.repo_root, rules))

    def test_non_dict_leaf_subdirs_entry_does_not_raise(self):
        rules = [
            {
                "root": "plugins",
                "root_dirs": ["skills"],
                "leaf_subdirs": {"skills": ["scripts"]},  # should be a dict, not a list
            }
        ]
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(
                    self.repo_root / "plugins" / "x" / "skills" / "y" / "z.md"
                )
            },
        }
        self.assertIsNone(sg.decide(event, self.repo_root, rules))

    def test_non_dict_leaf_subdirs_entry_at_flat_depth_degrades_to_no_constraint(self):
        # v3 regression: the entry-gate used to test the RAW (unsanitized) value, so a malformed
        # `leaf_subdirs.<head>` reached the "must be a directory" branch anyway and denied
        # unconditionally for a 2-segment write — contradicting "malformed = ignored, never
        # misread". Must behave exactly like `leaf_subdirs` omitting `head` entirely: no decision.
        rules = [
            {
                "root": "plugins",
                "root_dirs": ["skills"],
                "leaf_subdirs": {"skills": ["scripts"]},  # should be a dict, not a list
            }
        ]
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "plugins" / "x" / "skills" / "y.md")
            },
        }
        self.assertIsNone(sg.decide(event, self.repo_root, rules))

    def test_malformed_value_emits_a_stderr_warning(self):
        buf = io.StringIO()
        rules = [{"root": "plugins", "forbidden_dirs": "commands"}]
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.repo_root / "plugins" / "x" / "c" / "y.py")
            },
        }
        with contextlib.redirect_stderr(buf):
            sg.decide(event, self.repo_root, rules)
        self.assertIn("forbidden_dirs", buf.getvalue())


def _run_hook(script: str, repo_root: Path, target: Path):
    import subprocess

    event = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    return subprocess.run(
        [sys.executable, script],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        # Real PATH kept (unlike an empty env) so `git` resolves and the walk-up genuinely runs,
        # rather than passing by luck of a swallowed OSError.
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo_root)},
    )


class TestOptIn(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.script = str(
            Path(__file__).parent.parent
            / "plugins"
            / "pc-core"
            / "hooks"
            / "structure_guard.py"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_hook_no_op_without_file_placement_policy(self):
        repo_root = Path(self._tmp.name)
        result = _run_hook(
            self.script,
            repo_root,
            repo_root / "plugins" / "pc-core" / "vendor" / "x.py",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        self.assertEqual(result.stderr.strip(), "")

    def test_run_hook_no_op_on_legacy_boolean_value(self):
        # A pre-redesign `"file_placement": true` (bool, not a list) must degrade to inert, never
        # raise — the type check in run_hook() is what guarantees that.
        repo_root = Path(self._tmp.name)
        (repo_root / ".claude").mkdir()
        (repo_root / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"file_placement": True})
        )
        result = _run_hook(
            self.script,
            repo_root,
            repo_root / "plugins" / "pc-core" / "vendor" / "x.py",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        self.assertEqual(result.stderr.strip(), "")

    def test_local_override_beats_an_ancestor_file_placement(self):
        # NFR-0023-23's opt-in is a settings.pinky.json key like any other: a repo whose own
        # `plugins/` directory has nothing to do with Claude Code opts out locally, the same way
        # any settings.pinky.json key is overridden — no separate structural detection needed.
        workspace_root = Path(self._tmp.name)
        (workspace_root / ".claude").mkdir()
        (workspace_root / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"file_placement": PLUGIN_RULES})
        )
        child_repo = workspace_root / "other-app"
        (child_repo / ".claude").mkdir(parents=True)
        (child_repo / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"file_placement": []})
        )
        result = _run_hook(
            self.script,
            child_repo,
            child_repo / "plugins" / "pc-core" / "vendor" / "x.py",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_end_to_end_deny_with_real_settings_file(self):
        repo_root = Path(self._tmp.name)
        (repo_root / ".claude").mkdir()
        (repo_root / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"file_placement": PLUGIN_RULES})
        )
        result = _run_hook(
            self.script,
            repo_root,
            repo_root / "plugins" / "pc-core" / "vendor" / "x.py",
        )
        self.assertEqual(result.returncode, 0)
        decision = json.loads(result.stdout)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
