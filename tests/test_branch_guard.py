#!/usr/bin/env python3
"""Tests for plugins/pc-git/hooks/branch-policy/branch_guard.py — run with:
python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent / "plugins" / "pc-git" / "hooks" / "branch-policy"
    ),
)

import branch_guard as bg


class TestTargetPath(unittest.TestCase):
    def test_write_uses_file_path(self):
        self.assertEqual(
            bg.target_path("Write", {"file_path": "src/app.py"}), "src/app.py"
        )

    def test_notebook_edit_uses_notebook_path(self):
        self.assertEqual(
            bg.target_path("NotebookEdit", {"notebook_path": "nb.ipynb"}), "nb.ipynb"
        )


class TestRelativeTarget(unittest.TestCase):
    def test_absolute_inside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                bg.relative_target(str(root / "src" / "app.py"), root),
                str(Path("src") / "app.py"),
            )

    def test_already_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(bg.relative_target("src/app.py", root), "src/app.py")


class TestIsUnguarded(unittest.TestCase):
    """The real `branch_paths.sh` list — the function under test is the shell glob match."""

    def test_documentation_paths_are_unguarded(self):
        for path in (
            "docs/how-to/x.md",
            "README.md",
            "plugins/pc-statusline/README.md",
            "plugins/pc-statusline/docs/usage.md",
            ".claude-plugin/marketplace.json",
            "plugins/pc-craft/.claude-plugin/plugin.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(bg.is_unguarded(path))

    def test_behavior_carrying_paths_stay_guarded(self):
        for path in (
            "plugins/pc-core/skills/council/SKILL.md",
            "agentic-harness/rules/pc-core/adr.md",
            "plugins/pc-craft/.claude-plugin/hooks.json",
            ".claude-plugin/other.json",
            ".hidden/script.py",
            "notes.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(bg.is_unguarded(path))

    def test_profile_yaml_is_no_longer_exempt(self):
        self.assertFalse(bg.is_unguarded("profile.yaml"))


class TestCurrentBranch(unittest.TestCase):
    def test_reads_current_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            self.assertEqual(bg.current_branch(root), "main")

    def test_none_outside_any_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bg.current_branch(Path(tmp)))


class TestDecide(unittest.TestCase):
    """Real repos on disk — the class under test resolves policies and branch via subprocess,
    a fake in front of it would test nothing but the fake."""

    def _repo(self, tmp: Path, branch: str, strategy: str | None) -> Path:
        root = tmp / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=root, check=True)
        if strategy is not None:
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.pinky.json").write_text(
                json.dumps({"branch_workflow": strategy})
            )
        return root

    def test_denies_guarded_write_on_permanent_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), "main", "github flow")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(root / "app.py")},
            }
            decision = bg.decide(event, root)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_allows_unguarded_path_on_permanent_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), "main", "github flow")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(root / "docs" / "x.md")},
            }
            self.assertIsNone(bg.decide(event, root))

    def test_allows_guarded_path_on_topic_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), "feature/x", "github flow")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(root / "app.py")},
            }
            self.assertIsNone(bg.decide(event, root))

    def test_allows_when_strategy_undeclared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), "main", None)
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(root / "app.py")},
            }
            self.assertIsNone(bg.decide(event, root))

    def test_git_flow_guards_develop_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), "develop", "git flow")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(root / "app.py")},
            }
            decision = bg.decide(event, root)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_ignores_non_guarded_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(Path(tmp), "main", "github flow")
            event = {
                "tool_name": "Read",
                "tool_input": {"file_path": str(root / "app.py")},
            }
            self.assertIsNone(bg.decide(event, root))

    def test_target_in_no_git_repo_is_not_guarded(self):
        """The session's project dir is a repo on a permanent branch, but the write targets a
        sibling path (`_KANBAN/…`) that is inside no git repo. `resolve_repo_root` falls back to
        the project dir, and without the guard the target would inherit `main` and get wrongly
        denied."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)  # not a git repo
            repo = self._repo(workspace, "main", "github flow")
            outside = workspace / "_KANBAN" / "CHANTIER0025" / "card.md"
            outside.parent.mkdir(parents=True)
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(outside)},
            }
            self.assertIsNone(bg.decide(event, repo))


class TestMainEndToEnd(unittest.TestCase):
    """Real subprocess calls — same invocation shape Claude Code uses (JSON payload on stdin)."""

    SCRIPT = str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-git"
        / "hooks"
        / "branch-policy"
        / "branch_guard.py"
    )

    def _run(self, payload: dict, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=cwd,
            env={
                "CLAUDE_PROJECT_DIR": str(cwd),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
            check=False,
        )

    def test_denies_over_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.pinky.json").write_text(
                json.dumps({"branch_workflow": "github flow"})
            )
            result = self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(root / "app.py")},
                },
                cwd=root,
            )
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_silent_on_topic_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                ["git", "init", "-q", "-b", "feature/x"], cwd=root, check=True
            )
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.pinky.json").write_text(
                json.dumps({"branch_workflow": "github flow"})
            )
            result = self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(root / "app.py")},
                },
                cwd=root,
            )
            self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
