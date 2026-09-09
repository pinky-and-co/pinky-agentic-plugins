#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py
— run with: python3 -m unittest discover tests

Defines "done": given `<chantier-id> <card-slug> [repo-slug...]`, the script resolves the card, its
`repos:` and its `model:` through stop_handoff.py's CLI, resolves each slug to a local clone
(resolve_repos.sh), then per repo either runs `<mux> new-workspace` (inside cmux/wmux) or prints a
`cd ... && claude ...` line (outside both). The `claude` invocation carries `--model <card model>`
and `--advisor <one tier up>` (none for opus/fable). `[1m]` is never emitted.

`build_claude_command` / `build_spawn_command` / `build_manual_line` / `advisor_for` are pure and
get whole-output oracles; the two subprocess boundaries (`_hook`, `resolve_paths`) and the spawn
(`subprocess.run`) are the fakes.
"""

import contextlib
import io
import os
import shlex
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

_BOOTSTRAP = sw.SpawnTarget(
    "pinky-and-co/pinky-bootstrap", Path("/ws/bootstrap/pinky-bootstrap")
)


class TestAdvisorFor(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(sw.advisor_for("haiku"), "sonnet")
        self.assertEqual(sw.advisor_for("sonnet"), "opus")

    def test_ceiling_and_unknown_get_no_advisor(self):
        for m in ("opus", "fable", "", "sonnet[1m]", "gpt-4"):
            self.assertIsNone(sw.advisor_for(m))


class TestMultiplexer(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None) for k in ("CMUX_SURFACE_ID", "WMUX_SURFACE_ID")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_cmux(self):
        os.environ["CMUX_SURFACE_ID"] = "abc"
        self.assertEqual(sw.multiplexer(), "cmux")

    def test_wmux(self):
        os.environ["WMUX_SURFACE_ID"] = "abc"
        self.assertEqual(sw.multiplexer(), "wmux")

    def test_neither(self):
        self.assertIsNone(sw.multiplexer())


class TestBuildClaudeCommand(unittest.TestCase):
    def test_sonnet_card_gets_opus_advisor(self):
        self.assertEqual(
            sw.build_claude_command("api-fix", "0025", "fix", "sonnet", "opus"),
            "claude --name api-fix --model sonnet --advisor opus "
            '"/pc-kanban:chantier 0025 fix"',
        )

    def test_opus_card_has_no_advisor(self):
        self.assertEqual(
            sw.build_claude_command("x-y", "0009", "y", "opus", None),
            'claude --name x-y --model opus "/pc-kanban:chantier 0009 y"',
        )

    def test_no_model_leaves_model_flag_off(self):
        self.assertEqual(
            sw.build_claude_command("x-y", "0009", "y", "", None),
            'claude --name x-y "/pc-kanban:chantier 0009 y"',
        )

    def test_a_dollar_or_quote_in_the_name_is_neutralised(self):
        # `name` carries an unconstrained repo basename; shlex.quote must make it inert.
        got = sw.build_claude_command('a"$(x)', "0009", "y", "opus", None)
        self.assertEqual(
            got, 'claude --name \'a"$(x)\' --model opus "/pc-kanban:chantier 0009 y"'
        )


class TestBuildSpawnCommand(unittest.TestCase):
    def test_cmux_argv_is_pinned_whole(self):
        self.assertEqual(
            sw.build_spawn_command("cmux", _BOOTSTRAP, "0025", "spawn-x", "sonnet"),
            [
                "cmux",
                "new-workspace",
                "--cwd",
                "/ws/bootstrap/pinky-bootstrap",
                "--command",
                "claude --name pinky-bootstrap-spawn-x --model sonnet --advisor opus "
                '"/pc-kanban:chantier 0025 spawn-x"',
            ],
        )

    def test_wmux_only_the_binary_changes(self):
        cmux = sw.build_spawn_command("cmux", _BOOTSTRAP, "0025", "spawn-x", "haiku")
        wmux = sw.build_spawn_command("wmux", _BOOTSTRAP, "0025", "spawn-x", "haiku")
        self.assertEqual(wmux, ["wmux", *cmux[1:]])
        self.assertIn("--advisor", wmux[-1])
        self.assertIn("sonnet", wmux[-1])


class TestBuildManualLine(unittest.TestCase):
    def test_cd_then_claude(self):
        self.assertEqual(
            sw.build_manual_line(_BOOTSTRAP, "0025", "spawn-x", "opus"),
            "cd /ws/bootstrap/pinky-bootstrap && "
            "claude --name pinky-bootstrap-spawn-x --model opus "
            '"/pc-kanban:chantier 0025 spawn-x"',
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
            "--card-model": "sonnet\n",
        }
        self._saved_env = {
            k: os.environ.pop(k, None) for k in ("CMUX_SURFACE_ID", "WMUX_SURFACE_ID")
        }
        os.environ["CMUX_SURFACE_ID"] = "test"
        self._orig_resolve = sw.resolve_paths
        sw.resolve_paths = self._fake_resolve  # type: ignore[assignment]
        self.spawned: list[list[str]] = []
        self._orig_run = sw.subprocess.run
        sw.subprocess.run = self._fake_run  # type: ignore[assignment]

    def tearDown(self):
        sw.resolve_paths = self._orig_resolve  # type: ignore[assignment]
        sw.subprocess.run = self._orig_run  # type: ignore[assignment]
        for k, v in self._saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

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

    def test_dry_run_prints_shlex_joined_spawn_command_per_repo_and_spawns_nothing(
        self,
    ):
        rc, out, _ = self._run("--dry-run", "0099", "my-card")
        self.assertEqual(rc, 0)
        self.assertEqual(self.spawned, [])
        expected = "".join(
            shlex.join(
                sw.build_spawn_command(
                    "cmux", sw.SpawnTarget(s, Path(p)), "0099", "my-card", "sonnet"
                )
            )
            + "\n"
            for s, p in (("org/one", "/clones/one"), ("org/two", "/clones/two"))
        )
        self.assertEqual(out, expected)
        self.assertIn("--advisor opus", out)

    def test_scan_root_is_the_boards_workspace_root(self):
        self._run("--dry-run", "0099", "my-card")
        self.assertEqual(self.resolve_root, Path("/home/me/ws"))

    def test_repo_filter_spawns_only_that_repo(self):
        rc, _, _ = self._run("0099", "my-card", "org/two")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][:3], ["cmux", "new-workspace", "--cwd"])
        self.assertIn("/clones/two", self.spawned[0])

    def test_outside_any_multiplexer_prints_manual_lines_and_spawns_nothing(self):
        os.environ.pop("CMUX_SURFACE_ID", None)
        rc, out, _ = self._run("0099", "my-card")
        self.assertEqual(rc, 0)
        self.assertEqual(self.spawned, [])
        self.assertIn("open a worker session yourself", out)
        self.assertIn("cd /clones/one && claude --name one-my-card", out)
        self.assertIn("cd /clones/two && claude --name two-my-card", out)

    def test_card_without_repos_aborts(self):
        self.hook_stdout["--card-repos"] = ""
        with self.assertRaises(SystemExit) as cm:
            self._run("0099", "my-card")
        self.assertIn("no repos", str(cm.exception))

    def test_unknown_model_aborts(self):
        self.hook_stdout["--card-model"] = "gpt-4\n"
        with self.assertRaises(SystemExit) as cm:
            self._run("0099", "my-card")
        self.assertIn("gpt-4", str(cm.exception))

    def test_empty_card_path_means_stale_install_not_a_traceback(self):
        self.hook_stdout["--card-path"] = ""
        with self.assertRaises(SystemExit) as cm:
            self._run("0099", "my-card")
        self.assertIn("stale", str(cm.exception))

    def test_no_1m_variant_ever_emitted(self):
        self.hook_stdout["--card-model"] = "sonnet\n"
        _, out, _ = self._run("--dry-run", "0099", "my-card")
        self.assertNotIn("[1m]", out)

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
