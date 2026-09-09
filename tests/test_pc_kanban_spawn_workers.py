#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py
— run with: python3 -m unittest discover tests

Defines "done": given `<chantier-id> <card-slug> [repo-slug...]`, the script resolves the card and
its `repos:` through stop_handoff.py's CLI, resolves each slug to a local clone (resolve_repos.sh),
and emits one `cmux new-workspace` per repo — `--dry-run` prints the argv instead of spawning. A
missing/ambiguous card, an unknown repo slug, or a card with no `repos:` each abort.

`build_spawn_command` and `select_repos` are pure and get a whole-output oracle; the two
subprocess boundaries (`_hook` → stop_handoff.py, `resolve_paths` → resolve_repos.sh) and the cmux
spawn (`subprocess.run`) are the fakes.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

SCRIPTS = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "spawn-workers"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import pc_kanban_spawn_workers as sw  # noqa: E402


class TestBuildSpawnCommand(unittest.TestCase):
    def test_argv_is_pinned_whole(self):
        target = sw.SpawnTarget(
            "pinky-and-co/pinky-bootstrap", Path("/ws/bootstrap/pinky-bootstrap")
        )
        inner = (
            'claude -n "pinky-bootstrap-spawn-worker-sessions-par-repo" '
            '"/pc-kanban:chantier 0025 spawn-worker-sessions-par-repo"'
        )
        self.assertEqual(
            sw.build_spawn_command(target, "0025", "spawn-worker-sessions-par-repo"),
            [
                "cmux",
                "new-workspace",
                "--cwd",
                "/ws/bootstrap/pinky-bootstrap",
                "--name",
                "pinky-bootstrap-spawn-worker-sessions-par-repo",
                "--command",
                inner,
            ],
        )


class TestSelectRepos(unittest.TestCase):
    def test_no_filter_returns_all_in_declared_order(self):
        self.assertEqual(sw.select_repos(["org/a", "org/b"], []), ["org/a", "org/b"])

    def test_filter_preserves_declared_order_not_argument_order(self):
        self.assertEqual(sw.select_repos(["org/a", "org/b"], ["org/b"]), ["org/b"])

    def test_unknown_slug_aborts(self):
        with self.assertRaises(SystemExit) as cm:
            sw.select_repos(["org/a"], ["org/x"])
        self.assertIn("org/x", str(cm.exception))


class TestBoardWorkspaceRoot(unittest.TestCase):
    def test_parent_of_the_kanban_ancestor(self):
        card = Path("/home/me/ws/_KANBAN/CHANTIER0099-x/3-in-progress/c.md")
        self.assertEqual(sw.board_workspace_root(card), Path("/home/me/ws"))

    def test_no_kanban_ancestor_aborts(self):
        with self.assertRaises(SystemExit):
            sw.board_workspace_root(Path("/tmp/loose/c.md"))


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class TestMain(unittest.TestCase):
    def setUp(self):
        self.card_path = (
            "/home/me/ws/_KANBAN/CHANTIER0099-demo/2-selected-for-dev/my-card.md"
        )
        # keyed by the stop_handoff.py mode flag; the real _hook() runs against this fake
        # subprocess so its stale-copy guard is exercised too.
        self.hook_stdout = {
            "--card-path": f"{self.card_path}\n",
            "--card-repos": "org/one\norg/two\n",
        }
        self._orig_resolve = sw.resolve_paths
        sw.resolve_paths = self._fake_resolve  # type: ignore[assignment]
        self.spawned: list[list[str]] = []
        self._orig_run = sw.subprocess.run
        sw.subprocess.run = self._fake_run  # type: ignore[assignment]

    def tearDown(self):
        sw.resolve_paths = self._orig_resolve  # type: ignore[assignment]
        sw.subprocess.run = self._orig_run  # type: ignore[assignment]

    def _fake_resolve(self, workspace_root, slugs):
        self.resolve_root = workspace_root
        return [sw.SpawnTarget(s, Path(f"/clones/{s.split('/')[-1]}")) for s in slugs]

    def _fake_run(self, argv, **_kwargs):
        if argv[:2] == [sys.executable, str(sw._HOOK)]:
            return _Result(stdout=self.hook_stdout.get(argv[2], ""))
        self.spawned.append(list(argv))
        return _Result()

    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sw.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_prints_both_command_lines_and_spawns_nothing(self):
        rc, out, _ = self._run("--dry-run", "0099", "my-card")
        self.assertEqual(rc, 0)
        self.assertEqual(self.spawned, [])
        line_one = (
            "cmux new-workspace --cwd /clones/one --name one-my-card --command "
            '\'claude -n "one-my-card" "/pc-kanban:chantier 0099 my-card"\''
        )
        line_two = (
            "cmux new-workspace --cwd /clones/two --name two-my-card --command "
            '\'claude -n "two-my-card" "/pc-kanban:chantier 0099 my-card"\''
        )
        self.assertEqual(out, f"{line_one}\n{line_two}\n")

    def test_scan_root_is_the_boards_workspace_root(self):
        self._run("--dry-run", "0099", "my-card")
        self.assertEqual(self.resolve_root, Path("/home/me/ws"))

    def test_repo_filter_spawns_only_that_repo(self):
        rc, _, _ = self._run("0099", "my-card", "org/two")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][:3], ["cmux", "new-workspace", "--cwd"])
        self.assertIn("/clones/two", self.spawned[0])

    def test_card_without_repos_aborts(self):
        self.hook_stdout["--card-repos"] = ""
        with self.assertRaises(SystemExit) as cm:
            self._run("0099", "my-card")
        self.assertIn("no repos", str(cm.exception))

    def test_empty_card_path_means_stale_install_not_a_traceback(self):
        self.hook_stdout["--card-path"] = ""
        with self.assertRaises(SystemExit) as cm:
            self._run("0099", "my-card")
        self.assertIn("stale", str(cm.exception))

    def test_too_few_args_is_usage_error(self):
        rc, _, err = self._run("0099")
        self.assertEqual(rc, 2)
        self.assertIn("Usage:", err)

    def test_slug_with_shell_metacharacters_is_rejected_before_any_subprocess(self):
        with self.assertRaises(SystemExit):
            self._run("0099", 'my-card"; rm -rf ~')
        with self.assertRaises(SystemExit):
            self._run("not-an-id", "my-card")


if __name__ == "__main__":
    unittest.main()
