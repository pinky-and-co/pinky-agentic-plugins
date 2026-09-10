#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py
— run with: python3 -m unittest discover tests

Defines "done": given `<chantier-id> <card-slug> [repo-slug...]`, the script resolves the card, its
`repos:` and its `model:` through stop_handoff.py's CLI, resolves each slug to a local clone
(resolve_repos.sh), then per repo either runs `<mux> new-workspace` (inside cmux/wmux) or prints a
`cd ... && claude ...` line (outside both). The `claude` line carries the card's tier verbatim as
`--model "<tier>[1m]"` (double-quoted — zsh globs an unquoted `[1m]`; plain `haiku`, no 1m variant)
and `--advisor` one tier above (`haiku`->sonnet, `sonnet`->opus, none for opus/fable).

`build_claude_command` / `build_spawn_command` / `build_manual_line` / `model_arg` / `advisor_for`
are pure and get whole-output oracles; the two subprocess boundaries (`_hook`, `resolve_paths`)
and the spawn (`subprocess.run`) are the fakes.
"""

import contextlib
import io
import os
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


class TestModelArg(unittest.TestCase):
    def test_tier_verbatim_as_the_1m_variant_where_one_exists(self):
        self.assertEqual(sw.model_arg("haiku"), "haiku")
        self.assertEqual(sw.model_arg("sonnet"), "sonnet[1m]")
        self.assertEqual(sw.model_arg("opus"), "opus[1m]")
        self.assertEqual(sw.model_arg("fable"), "fable[1m]")

    def test_empty_or_unknown_yields_no_model(self):
        self.assertIsNone(sw.model_arg(""))
        self.assertIsNone(sw.model_arg("gpt-4"))


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
    def test_sonnet_card(self):
        self.assertEqual(
            sw.build_claude_command("api-fix", "0025", "fix", "sonnet"),
            'claude --name api-fix --model "sonnet[1m]" --advisor "opus" '
            '"/pc-kanban:chantier 0025 fix"',
        )

    def test_haiku_card_runs_haiku_never_sonnet(self):
        self.assertEqual(
            sw.build_claude_command("x-y", "0009", "y", "haiku"),
            'claude --name x-y --model "haiku" --advisor "sonnet" '
            '"/pc-kanban:chantier 0009 y"',
        )

    def test_opus_card_no_advisor(self):
        self.assertEqual(
            sw.build_claude_command("x-y", "0009", "y", "opus"),
            'claude --name x-y --model "opus[1m]" "/pc-kanban:chantier 0009 y"',
        )

    def test_no_model_declared_is_a_bare_boot(self):
        self.assertEqual(
            sw.build_claude_command("x-y", "0009", "y", ""),
            'claude --name x-y "/pc-kanban:chantier 0009 y"',
        )

    def test_a_dollar_or_quote_in_the_name_is_neutralised(self):
        # `name` carries an unconstrained repo basename; shlex.quote must make it inert.
        got = sw.build_claude_command('a"$(x)', "0009", "y", "opus")
        self.assertEqual(
            got,
            'claude --name \'a"$(x)\' --model "opus[1m]" "/pc-kanban:chantier 0009 y"',
        )


class TestBuildSpawnCommand(unittest.TestCase):
    def test_cmux_argv_is_pinned_whole(self):
        self.assertEqual(
            sw.build_spawn_command("cmux", _BOOTSTRAP, "0025", "spawn-x", "opus"),
            [
                "cmux",
                "new-workspace",
                "--cwd",
                "/ws/bootstrap/pinky-bootstrap",
                "--command",
                'claude --name pinky-bootstrap-spawn-x --model "opus[1m]" '
                '"/pc-kanban:chantier 0025 spawn-x"',
            ],
        )

    def test_wmux_only_the_binary_changes(self):
        cmux = sw.build_spawn_command("cmux", _BOOTSTRAP, "0025", "spawn-x", "haiku")
        wmux = sw.build_spawn_command("wmux", _BOOTSTRAP, "0025", "spawn-x", "haiku")
        self.assertEqual(wmux, ["wmux", *cmux[1:]])
        self.assertIn('--model "haiku" --advisor "sonnet"', wmux[-1])


class TestBuildManualLine(unittest.TestCase):
    def test_cd_then_claude(self):
        self.assertEqual(
            sw.build_manual_line(_BOOTSTRAP, "0025", "spawn-x", "sonnet"),
            "cd /ws/bootstrap/pinky-bootstrap && "
            'claude --name pinky-bootstrap-spawn-x --model "sonnet[1m]" --advisor "opus" '
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
            "--card-model": "opus\n",
        }
        self._saved_env = {
            k: os.environ.pop(k, None) for k in ("CMUX_SURFACE_ID", "WMUX_SURFACE_ID")
        }
        os.environ["CMUX_SURFACE_ID"] = "test"
        self._orig_resolve = sw.resolve_paths
        sw.resolve_paths = self._fake_resolve  # type: ignore[assignment]
        self.spawned: list[list[str]] = []
        self.enabled: list[tuple[list[str], dict]] = []
        self.events: list[tuple[str, str]] = []  # ("enable"|"spawn", clone path)
        self.enable_rc = 0
        self.matched: list[str] = []
        self._orig_run = sw.subprocess.run
        sw.subprocess.run = self._fake_run  # type: ignore[assignment]
        self._orig_match = sw.matching_plugin_ids
        sw.matching_plugin_ids = lambda repo: list(self.matched)  # type: ignore[assignment]

    def tearDown(self):
        sw.resolve_paths = self._orig_resolve  # type: ignore[assignment]
        sw.subprocess.run = self._orig_run  # type: ignore[assignment]
        sw.matching_plugin_ids = self._orig_match  # type: ignore[assignment]
        for k, v in self._saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def _fake_resolve(self, workspace_root, slugs):
        self.resolve_root = workspace_root
        return [sw.SpawnTarget(s, Path(f"/clones/{s.split('/')[-1]}")) for s in slugs]

    def _fake_run(self, argv, **kwargs):
        if argv[:2] == [sys.executable, str(sw._HOOK)]:
            return _Result(stdout=self.hook_stdout.get(argv[2], ""))
        if list(argv[:3]) == ["claude", "plugin", "install"]:
            self.enabled.append((list(argv), kwargs))
            self.events.append(("enable", str(kwargs.get("cwd"))))
            return _Result(
                returncode=self.enable_rc, stderr="boom" if self.enable_rc else ""
            )
        self.spawned.append(list(argv))
        self.events.append(("spawn", argv[argv.index("--cwd") + 1]))
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
        # hand-pinned, not computed from build_spawn_command — this asserts the whole dry-run
        # surface (one shlex-joined `cmux` line per repo), independent of the builders.
        self.assertEqual(
            out,
            "cmux new-workspace --cwd /clones/one --command "
            '\'claude --name one-my-card --model "opus[1m]" '
            '"/pc-kanban:chantier 0099 my-card"\'\n'
            "cmux new-workspace --cwd /clones/two --command "
            '\'claude --name two-my-card --model "opus[1m]" '
            '"/pc-kanban:chantier 0099 my-card"\'\n',
        )

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

    def test_haiku_card_boots_haiku_not_sonnet(self):
        self.hook_stdout["--card-model"] = "haiku\n"
        _, out, _ = self._run("--dry-run", "0099", "my-card")
        self.assertIn('--model "haiku" --advisor "sonnet"', out)
        self.assertNotIn('--model "sonnet', out)

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

    def test_haiku_1m_alias_never_emitted(self):
        self.hook_stdout["--card-model"] = "haiku\n"
        _, out, _ = self._run("--dry-run", "0099", "my-card")
        self.assertNotIn("haiku[1m]", out)

    def test_too_few_args_is_usage_error(self):
        rc, _, err = self._run("0099")
        self.assertEqual(rc, 2)
        self.assertIn("Usage:", err)

    def test_slug_with_shell_metacharacters_is_rejected_before_any_subprocess(self):
        with self.assertRaises(SystemExit):
            self._run("0099", 'my-card"; rm -rf ~')
        with self.assertRaises(SystemExit):
            self._run("not-an-id", "my-card")

    def test_matched_project_plugins_are_enabled_per_repo_before_spawn(self):
        self.matched = ["pc-git@mp", "pc-kanban@mp"]
        rc, _, _ = self._run("0099", "my-card")
        self.assertEqual(rc, 0)
        # 2 plugins x 2 repos, each a project-scope install run with cwd on the clone
        self.assertEqual(len(self.enabled), 4)
        for argv, kwargs in self.enabled:
            self.assertEqual(argv[:3], ["claude", "plugin", "install"])
            self.assertEqual(argv[4:], ["--scope", "project", "--yes"])
            self.assertEqual(kwargs["timeout"], 15)
        # ordered: both enables for a repo, then its spawn, then the next repo
        self.assertEqual(
            self.events,
            [
                ("enable", "/clones/one"),
                ("enable", "/clones/one"),
                ("spawn", "/clones/one"),
                ("enable", "/clones/two"),
                ("enable", "/clones/two"),
                ("spawn", "/clones/two"),
            ],
        )

    def test_dry_run_enables_nothing(self):
        self.matched = ["pc-git@mp"]
        self._run("--dry-run", "0099", "my-card")
        self.assertEqual(self.enabled, [])

    def test_a_failed_enable_is_logged_not_fatal(self):
        self.matched = ["pc-git@mp"]
        self.enable_rc = 1
        rc, _, err = self._run("0099", "my-card", "org/one")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("pc-git@mp", err)

    def test_a_failed_enable_is_not_retried(self):
        self.matched = ["pc-git@mp"]
        self.enable_rc = 1
        self._run("0099", "my-card")
        self.assertEqual(len(self.enabled), 2)  # one attempt per repo, no retry


if __name__ == "__main__":
    unittest.main()
