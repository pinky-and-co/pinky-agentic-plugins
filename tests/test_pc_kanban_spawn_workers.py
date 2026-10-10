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

Default (no `--cmux`) mode: `repo_conventions_block` / `generated_card_block` /
`build_worker_prompt` / `effective_model` compose the prompt handed to a `card-worker` Agent call —
also pure, also whole-output oracles, with `shutil.which`/the temp repo's own files as the fakes.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

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


class TestEffortInClaudeCommand(unittest.TestCase):
    def test_effort_is_added_before_the_boot_prompt(self):
        self.assertEqual(
            sw.build_claude_command("x-y", "0009", "y", "opus", "low"),
            'claude --name x-y --model "opus[1m]" --effort low '
            '"/pc-kanban:chantier 0009 y"',
        )

    def test_spawn_command_and_manual_line_carry_it(self):
        argv = sw.build_spawn_command("cmux", _BOOTSTRAP, "0025", "s", "opus", "max")
        self.assertIn("--effort max", argv[-1])
        self.assertIn(
            "--effort max", sw.build_manual_line(_BOOTSTRAP, "0025", "s", "opus", "max")
        )

    def test_no_effort_no_flag(self):
        self.assertNotIn("--effort", sw.build_claude_command("x", "0009", "y", "opus"))


class TestEffectiveEffort(unittest.TestCase):
    def test_override_wins_then_card_then_nothing(self):
        self.assertEqual(sw.effective_effort("low", "high"), "high")
        self.assertEqual(sw.effective_effort("low", None), "low")
        self.assertEqual(sw.effective_effort("", None), "")


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


class TestRepoConventionsBlock(unittest.TestCase):
    def test_whole_block_pinned_tests_dir_uv_and_prek(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "tests").mkdir()
            (repo / "uv.lock").write_text("")
            with mock.patch.object(
                sw.shutil,
                "which",
                side_effect=lambda name: (
                    f"/usr/bin/{name}" if name in ("prek", "uv") else None
                ),
            ):
                block = sw.repo_conventions_block(repo)
            self.assertEqual(
                block,
                "Repo conventions, probed from disk — do not retype these from memory:\n"
                "- Python tests: `tests/` at the repo root.\n"
                "- Run tests: `uv run pytest <file> -q`\n"
                "- Run gates: `prek run --files <path1> <path2> ...` — write each touched path "
                "literally, one argument per path, never via a variable or a pipe; must all "
                "pass.\n"
                f"- {sw._WORKER_INVARIANTS}",
            )

    def test_no_tests_dir_no_uv_no_gate_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch.object(sw.shutil, "which", return_value=None):
                block = sw.repo_conventions_block(repo)
            self.assertEqual(
                block,
                "Repo conventions, probed from disk — do not retype these from memory:\n"
                "- Python tests: colocated with the code under test (no top-level `tests/` "
                "found).\n"
                "- Run tests: `pytest <file> -q`\n"
                f"- {sw._WORKER_INVARIANTS}",
            )

    def test_pre_commit_used_when_prek_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch.object(
                sw.shutil,
                "which",
                side_effect=lambda name: (
                    "/usr/bin/pre-commit" if name == "pre-commit" else None
                ),
            ):
                self.assertEqual(
                    sw.repo_gate_command(repo),
                    "pre-commit run --files <path1> <path2> ...",
                )

    def test_gate_line_never_names_a_touched_files_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            for which in ("prek", "pre-commit"):
                with mock.patch.object(
                    sw.shutil,
                    "which",
                    side_effect=lambda n, w=which: f"/usr/bin/{n}" if n == w else None,
                ):
                    block = sw.repo_conventions_block(Path(tmp))
                self.assertNotIn("<touched files>", block)
                self.assertIn("one argument per path, never via a variable", block)

    def test_generated_gate_command_is_covered_by_the_card_worker_allowlist(self):
        agent = SCRIPTS.parent.parent.parent / "agents" / "card-worker.md"
        tools_line = next(
            line for line in agent.read_text().splitlines() if line.startswith("tools:")
        )
        allowed = [
            e.strip()[len("Bash(") : -1]
            for e in tools_line[len("tools:") :].split(", ")
            if e.strip().startswith("Bash(")
        ]
        for which in ("prek", "pre-commit"):
            with mock.patch.object(
                sw.shutil,
                "which",
                side_effect=lambda n, w=which: f"/x/{n}" if n == w else None,
            ):
                gate = sw.repo_gate_command(Path("/ws/repo"))
            self.assertTrue(
                any(
                    a.endswith(":*") and gate.startswith(a[: -len(":*")] + " ")
                    for a in allowed
                ),
                f"{gate!r} not covered by {allowed}",
            )


class TestGeneratedCardBlock(unittest.TestCase):
    def test_whole_block_pinned_no_arbitrage(self):
        self.assertEqual(
            sw.generated_card_block(Path("/ws/repo/_KANBAN/c.md"), "org/repo"),
            "Card to work:\n- Card: /ws/repo/_KANBAN/c.md\n- Repo: org/repo",
        )

    def test_whole_block_pinned_with_arbitrage(self):
        self.assertEqual(
            sw.generated_card_block(
                Path("/ws/repo/_KANBAN/c.md"), "org/repo", "pick a branch name"
            ),
            "Card to work:\n"
            "- Card: /ws/repo/_KANBAN/c.md\n"
            "- Repo: org/repo\n"
            "- Judgment call to resolve: pick a branch name",
        )


class TestBuildWorkerPrompt(unittest.TestCase):
    def test_contains_both_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch.object(sw.shutil, "which", return_value=None):
                prompt = sw.build_worker_prompt(
                    Path("/ws/repo/_KANBAN/c.md"),
                    repo,
                    "org/repo",
                    "pick a branch name",
                )
                self.assertEqual(
                    prompt,
                    f"{sw.repo_conventions_block(repo)}\n\n"
                    f"{sw.generated_card_block(Path('/ws/repo/_KANBAN/c.md'), 'org/repo', 'pick a branch name')}",
                )
            self.assertIn("Repo conventions, probed from disk", prompt)
            self.assertIn("Card to work:", prompt)


def _write_card(path: Path, cites: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"---\nrequirements: [{cites}]\n---\n# {path.stem}\n"
    if rows:
        body += "\n## Requirements\n\n| Id | P | E | C |\n|---|---|---|---|\n"
        body += "".join(f"{r}\n" for r in rows)
    path.write_text(body, encoding="utf-8")


class TestBuildWorkerPromptRequirementRows(unittest.TestCase):
    def _prompt(self, tmp: str, card: Path) -> tuple[str, str]:
        """Return the prompt and the conventions block, both probed with no tool on PATH."""
        repo = Path(tmp) / "repo"
        repo.mkdir()
        with mock.patch.object(sw.shutil, "which", return_value=None):
            return (
                sw.build_worker_prompt(card, repo, "org/repo"),
                sw.repo_conventions_block(repo),
            )

    def test_cited_row_defined_elsewhere_is_added_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            chantier = Path(tmp) / "_KANBAN" / "CHANTIER0025-demo"
            row = "| PKG-0025-1 | MUST | the statement | the check |"
            _write_card(chantier / "1-todo" / "definer.md", "", [row])
            card = chantier / "3-in-progress" / "citer.md"
            _write_card(card, "PKG-0025-1, PKG-0025-99", [])
            prompt, conventions = self._prompt(tmp, card)
            self.assertEqual(
                prompt,
                f"{conventions}\n\n"
                f"Card to work:\n- Card: {card}\n- Repo: org/repo\n"
                "- Requirements cited by the card, defined in other cards (full rows):\n"
                f"  {row}",
            )

    def test_card_defining_its_own_ids_gets_the_unchanged_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            chantier = Path(tmp) / "_KANBAN" / "CHANTIER0025-demo"
            row = "| PKG-0025-1 | MUST | the statement | the check |"
            card = chantier / "3-in-progress" / "own.md"
            _write_card(card, "PKG-0025-1", [row])
            _write_card(chantier / "1-todo" / "other.md", "", [row])
            prompt, conventions = self._prompt(tmp, card)
            self.assertEqual(
                prompt,
                f"{conventions}\n\n{sw.generated_card_block(card, 'org/repo')}",
            )


class TestCitedRowsAcrossCards(unittest.TestCase):
    _MARK = "  (definitions differ across cards — verify with the user)"

    def _lines(self, tmp: str, card: Path) -> str:
        """Return the generated block of the prompt, the part after the conventions."""
        repo = Path(tmp) / "repo"
        repo.mkdir()
        with mock.patch.object(sw.shutil, "which", return_value=None):
            prompt = sw.build_worker_prompt(card, repo, "org/repo")
        return prompt[prompt.index("Card to work:") :]

    def _head(self, card: Path) -> str:
        return (
            f"Card to work:\n- Card: {card}\n- Repo: org/repo\n"
            "- Requirements cited by the card, defined in other cards (full rows):\n"
        )

    def test_differing_definitions_list_every_row_after_a_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            chantier = Path(tmp) / "_KANBAN" / "CHANTIER0025-demo"
            r1 = "| PKG-0025-1 | MUST | s | c1 |"
            r2 = "| PKG-0025-1 | MUST | s | c2 |"
            _write_card(chantier / "0-ideas" / "a.md", "", [r1])
            _write_card(chantier / "1-todo" / "b.md", "", [r2])
            _write_card(chantier / "2-selected-for-dev" / "c.md", "", [r1])
            card = chantier / "3-in-progress" / "citer.md"
            _write_card(card, "PKG-0025-1", [])
            self.assertEqual(
                self._lines(tmp, card),
                self._head(card) + f"{self._MARK}\n  {r1}\n  {r2}",
            )

    def test_identical_definitions_across_columns_give_one_row_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            chantier = Path(tmp) / "_KANBAN" / "CHANTIER0025-demo"
            row = "| PKG-0025-1 | MUST | s | c |"
            _write_card(chantier / "0-ideas" / "a.md", "", [row])
            _write_card(chantier / "3-in-progress" / "b.md", "", [row])
            card = chantier / "1-todo" / "citer.md"
            _write_card(card, "PKG-0025-1", [])
            self.assertEqual(self._lines(tmp, card), self._head(card) + f"  {row}")

    def test_several_cited_ids_keep_the_citing_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            chantier = Path(tmp) / "_KANBAN" / "CHANTIER0025-demo"
            r1 = "| PKG-0025-1 | MUST | s | c |"
            r2 = "| PKG-0025-2 | MUST | s | c |"
            _write_card(chantier / "1-todo" / "a.md", "", [r1, r2])
            card = chantier / "3-in-progress" / "citer.md"
            _write_card(card, "PKG-0025-2, PKG-0025-1", [])
            self.assertEqual(
                self._lines(tmp, card), self._head(card) + f"  {r2}\n  {r1}"
            )

    def test_unreadable_sibling_card_does_not_fail_the_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            chantier = Path(tmp) / "_KANBAN" / "CHANTIER0025-demo"
            row = "| PKG-0025-1 | MUST | s | c |"
            _write_card(chantier / "1-todo" / "good.md", "", [row])
            (chantier / "0-ideas").mkdir(parents=True)
            (chantier / "0-ideas" / "broken.md").symlink_to(Path(tmp) / "missing.md")
            card = chantier / "3-in-progress" / "citer.md"
            _write_card(card, "PKG-0025-1", [])
            self.assertEqual(self._lines(tmp, card), self._head(card) + f"  {row}")


class TestEffectiveModel(unittest.TestCase):
    def test_requested_wins_over_frontmatter(self):
        self.assertEqual(sw.effective_model("opus", "sonnet"), "sonnet")

    def test_falls_back_to_frontmatter_when_none_requested(self):
        self.assertEqual(sw.effective_model("opus", None), "opus")

    def test_empty_requested_falls_back_too(self):
        self.assertEqual(sw.effective_model("opus", ""), "opus")


class TestExtractValuedFlag(unittest.TestCase):
    def test_absent_flag(self):
        value, rest = sw._extract_valued_flag(["a", "b"], "--model")
        self.assertIsNone(value)
        self.assertEqual(rest, ["a", "b"])

    def test_present_flag_extracted(self):
        value, rest = sw._extract_valued_flag(
            ["a", "--model", "sonnet", "b"], "--model"
        )
        self.assertEqual(value, "sonnet")
        self.assertEqual(rest, ["a", "b"])

    def test_flag_with_no_value_aborts(self):
        with self.assertRaises(SystemExit):
            sw._extract_valued_flag(["a", "--model"], "--model")


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


class _MainHarness:
    """setUp/tearDown and helpers shared by every test class that drives `sw.main`."""

    def setUp(self):
        self.card_path = (
            "/home/me/ws/_KANBAN/CHANTIER0099-demo/2-selected-for-dev/my-card.md"
        )
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


class TestMain(_MainHarness, unittest.TestCase):
    def test_dry_run_prints_shlex_joined_spawn_command_per_repo_and_spawns_nothing(
        self,
    ):
        rc, out, _ = self._run("--cmux", "--dry-run", "0099", "my-card")
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
        rc, _, _ = self._run("--cmux", "0099", "my-card", "org/two")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][:3], ["cmux", "new-workspace", "--cwd"])
        self.assertIn("/clones/two", self.spawned[0])

    def test_outside_any_multiplexer_prints_manual_lines_and_spawns_nothing(self):
        os.environ.pop("CMUX_SURFACE_ID", None)
        rc, out, _ = self._run("--cmux", "0099", "my-card")
        self.assertEqual(rc, 0)
        self.assertEqual(self.spawned, [])
        self.assertIn("open a worker session yourself", out)
        self.assertIn("cd /clones/one && claude --name one-my-card", out)
        self.assertIn("cd /clones/two && claude --name two-my-card", out)

    def test_haiku_card_boots_haiku_not_sonnet(self):
        self.hook_stdout["--card-model"] = "haiku\n"
        _, out, _ = self._run("--cmux", "--dry-run", "0099", "my-card")
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
        _, out, _ = self._run("--cmux", "--dry-run", "0099", "my-card")
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
        rc, _, _ = self._run("--cmux", "0099", "my-card")
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
        self._run("--cmux", "--dry-run", "0099", "my-card")
        self.assertEqual(self.enabled, [])

    def test_a_failed_enable_is_logged_not_fatal(self):
        self.matched = ["pc-git@mp"]
        self.enable_rc = 1
        rc, _, err = self._run("--cmux", "0099", "my-card", "org/one")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("pc-git@mp", err)

    def test_a_failed_enable_is_not_retried(self):
        self.matched = ["pc-git@mp"]
        self.enable_rc = 1
        self._run("--cmux", "0099", "my-card")
        self.assertEqual(len(self.enabled), 2)  # one attempt per repo, no retry

    def test_default_prints_worker_prompt_per_repo_and_spawns_nothing(self):
        rc, out, _ = self._run("0099", "my-card")
        self.assertEqual(rc, 0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.enabled, [])
        self.assertIn("=== worker: one-my-card ===", out)
        self.assertIn("=== worker: two-my-card ===", out)
        self.assertIn("model: opus", out)
        self.assertIn(str(self.card_path), out)

    def test_default_model_override_wins_over_card_frontmatter(self):
        self.hook_stdout["--card-model"] = "opus\n"
        rc, out, _ = self._run("--model", "sonnet", "0099", "my-card", "org/one")
        self.assertEqual(rc, 0)
        self.assertIn("model: sonnet", out)
        self.assertNotIn("model: opus", out)

    def test_default_unknown_model_override_aborts(self):
        with self.assertRaises(SystemExit) as cm:
            self._run("--model", "gpt-4", "0099", "my-card")
        self.assertIn("gpt-4", str(cm.exception))

    def test_default_arbitrage_reaches_the_prompt(self):
        _, out, _ = self._run(
            "--arbitrage", "pick a branch name", "0099", "my-card", "org/one"
        )
        self.assertIn("pick a branch name", out)

    def test_default_prints_the_card_effort(self):
        self.hook_stdout["--card-effort"] = "low\n"
        _, out, _ = self._run("0099", "my-card", "org/one")
        self.assertIn("model: opus\neffort: low\n--- prompt ---", out)

    def test_default_prints_no_effort_line_without_one(self):
        _, out, _ = self._run("0099", "my-card", "org/one")
        self.assertNotIn("effort:", out)

    def test_effort_flag_overrides_the_card(self):
        self.hook_stdout["--card-effort"] = "low\n"
        _, out, _ = self._run("--effort", "high", "0099", "my-card", "org/one")
        self.assertIn("effort: high", out)
        self.assertNotIn("effort: low", out)

    def test_invalid_effort_flag_aborts_naming_the_values(self):
        with self.assertRaises(SystemExit) as cm:
            self._run("--effort", "auto", "0099", "my-card")
        self.assertIn("'auto'", str(cm.exception))
        self.assertIn("xhigh", str(cm.exception))

    def test_invalid_card_effort_aborts(self):
        self.hook_stdout["--card-effort"] = "auto\n"
        with self.assertRaises(SystemExit):
            self._run("0099", "my-card")

    def test_cmux_command_carries_the_effort(self):
        self.hook_stdout["--card-effort"] = "low\n"
        _, out, _ = self._run("--cmux", "--dry-run", "0099", "my-card", "org/one")
        self.assertIn('--model "opus[1m]" --effort low "/pc-kanban', out)

    def test_cmux_effort_flag_overrides_the_card(self):
        self.hook_stdout["--card-effort"] = "low\n"
        _, out, _ = self._run(
            "--cmux", "--dry-run", "--effort", "max", "0099", "my-card", "org/one"
        )
        self.assertIn("--effort max", out)
        self.assertNotIn("--effort low", out)


_CARD = Path("/ws/repo/_KANBAN/c.md")
_LEGEND = (
    "  - strict: reproduce identically; if that cannot be met, say so in the report "
    "instead of deviating silently\n"
    "  - guided: follow the intent, justify every deviation in the report\n"
    "  - free: the mockup is only an inspiration"
)


class TestFidelityLevels(unittest.TestCase):
    def test_an_absent_axis_is_guided(self):
        self.assertEqual(
            sw.fidelity_levels([]),
            {
                "layout": "guided",
                "design": "guided",
                "content": "guided",
                "behavior": "guided",
            },
        )

    def test_declared_axes_override_the_default_in_axis_order(self):
        self.assertEqual(
            sw.fidelity_levels(["design=free", "layout=strict"]),
            {
                "layout": "strict",
                "design": "free",
                "content": "guided",
                "behavior": "guided",
            },
        )

    def test_spaces_around_the_equals_sign_are_ignored(self):
        self.assertEqual(sw.fidelity_levels(["layout = strict"])["layout"], "strict")

    def test_an_unknown_axis_or_level_aborts_naming_the_values(self):
        with self.assertRaises(SystemExit) as cm:
            sw.fidelity_levels(["color=strict"])
        self.assertIn("layout", str(cm.exception))
        with self.assertRaises(SystemExit) as cm:
            sw.fidelity_levels(["layout=exact"])
        self.assertIn("strict", str(cm.exception))


class TestMockupBlock(unittest.TestCase):
    def test_whole_block_pinned_with_a_partial_fidelity_list(self):
        levels = sw.fidelity_levels(["layout=strict", "design=free"])
        self.assertEqual(
            sw.generated_card_block(
                _CARD, "org/repo", mockup=Path("/m/poc.html"), levels=levels
            ),
            "Card to work:\n"
            f"- Card: {_CARD}\n"
            "- Repo: org/repo\n"
            "- Mockup (the rendering spec — read it in full before writing any code): "
            "/m/poc.html\n"
            "- Mockup fidelity per axis: layout=strict, design=free, content=guided, "
            "behavior=guided\n"
            f"{_LEGEND}\n"
            "- Mockup report: hand back a table `mockup element | implementation | status` "
            "(status: done | deviation | missing) covering the strict and guided axes "
            "(layout, content, behavior); for a guided axis, add the justified deviation.",
        )

    def test_a_card_with_a_mockup_and_no_list_has_four_guided_axes(self):
        block = sw.generated_card_block(
            _CARD, "org/repo", mockup=Path("/m"), levels=sw.fidelity_levels([])
        )
        self.assertEqual(
            block,
            "Card to work:\n"
            f"- Card: {_CARD}\n"
            "- Repo: org/repo\n"
            "- Mockup (the rendering spec — read it in full before writing any code): /m\n"
            "- Mockup fidelity per axis: layout=guided, design=guided, content=guided, "
            "behavior=guided\n"
            f"{_LEGEND}\n"
            "- Mockup report: hand back a table `mockup element | implementation | status` "
            "(status: done | deviation | missing) covering the strict and guided axes "
            "(layout, design, content, behavior); for a guided axis, add the justified "
            "deviation.",
        )

    def test_all_free_asks_for_no_table(self):
        levels = {a: "free" for a in sw.AXES}
        block = sw.generated_card_block(
            _CARD, "org/repo", mockup=Path("/m"), levels=levels
        )
        self.assertNotIn("Mockup report", block)
        self.assertIn("layout=free, design=free, content=free, behavior=free", block)

    def test_no_mockup_leaves_the_block_unchanged(self):
        self.assertEqual(
            sw.generated_card_block(_CARD, "org/repo", "x", ["  row"]),
            f"Card to work:\n- Card: {_CARD}\n- Repo: org/repo\n"
            "- Judgment call to resolve: x\n"
            "- Requirements cited by the card, defined in other cards (full rows):\n"
            "  row",
        )

    def test_the_mockup_block_follows_arbitrage_and_cited_rows(self):
        block = sw.generated_card_block(
            _CARD,
            "org/repo",
            "x",
            ["  row"],
            mockup=Path("/m"),
            levels=sw.fidelity_levels([]),
        )
        self.assertLess(block.index("  row"), block.index("- Mockup ("))


def _zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)


class TestPrepareMockup(unittest.TestCase):
    def test_a_plain_path_is_returned_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "poc.html"
            html.write_text("x")
            self.assertEqual(sw.prepare_mockup(html), html)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["poc.html"])

    def test_a_zip_is_extracted_into_its_sibling_folder_then_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "poc.zip"
            _zip(zpath, {"index.html": "v1", "css/a.css": "a"})
            target = sw.prepare_mockup(zpath)
            self.assertEqual(target, Path(tmp) / "poc")
            self.assertEqual((target / "index.html").read_text(), "v1")
            self.assertEqual((target / "css" / "a.css").read_text(), "a")
            _zip(zpath, {"index.html": "v2"})
            (target / "marker").write_text("kept")
            self.assertEqual(sw.prepare_mockup(zpath), target)
            self.assertEqual((target / "index.html").read_text(), "v1")
            self.assertEqual((target / "marker").read_text(), "kept")

    def test_an_entry_leaving_the_target_is_refused_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            zpath = root / "evil.zip"
            _zip(zpath, {"ok.txt": "fine", "../x": "escaped"})
            with self.assertRaises(SystemExit) as cm:
                sw.prepare_mockup(zpath)
            self.assertIn("../x", str(cm.exception))
            self.assertEqual([p.name for p in root.iterdir()], ["evil.zip"])

    def test_an_uppercase_zip_suffix_is_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "poc.ZIP"
            _zip(zpath, {"index.html": "x"})
            target = sw.prepare_mockup(zpath)
            self.assertEqual(target, Path(tmp) / "poc")
            self.assertTrue((target / "index.html").is_file())

    def test_a_failed_extraction_leaves_nothing_behind(self):
        def half_extract(self_, path=None, members=None, pwd=None):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "partial").write_text("cut")
            raise OSError("disk full")

        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "poc.zip"
            _zip(zpath, {"index.html": "x"})
            with mock.patch.object(sw.zipfile.ZipFile, "extractall", half_extract):
                with self.assertRaises(OSError):
                    sw.prepare_mockup(zpath)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["poc.zip"])

    def test_a_sibling_that_is_a_file_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "poc.zip"
            _zip(zpath, {"index.html": "x"})
            (Path(tmp) / "poc").write_text("a file")
            with self.assertRaises(SystemExit) as cm:
                sw.prepare_mockup(zpath)
            self.assertIn("not a directory", str(cm.exception))

    def test_a_file_that_is_not_a_zip_aborts_in_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "poc.zip"
            zpath.write_text("not an archive")
            with self.assertRaises(SystemExit) as cm:
                sw.prepare_mockup(zpath)
            self.assertIn("not a valid zip", str(cm.exception))
            self.assertNotIn("\n", str(cm.exception))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["poc.zip"])

    def test_an_absolute_entry_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            outside = Path(tmp) / "abs.txt"
            zpath = root / "evil.zip"
            _zip(zpath, {str(outside): "escaped"})
            with self.assertRaises(SystemExit):
                sw.prepare_mockup(zpath)
            self.assertFalse(outside.exists())
            self.assertEqual([p.name for p in root.iterdir()], ["evil.zip"])


class TestMainMockup(_MainHarness, unittest.TestCase):
    """Same harness as `TestMain`; the card now declares a mockup."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_default_prints_the_mockup_brief_for_every_repo(self):
        html = Path(self._tmp.name) / "poc.html"
        html.write_text("x")
        self.hook_stdout["--card-mockup"] = f"{html}\n"
        self.hook_stdout["--card-mockup-fidelity"] = "layout=strict\ndesign=free\n"
        _, out, _ = self._run("0099", "my-card")
        for repo in ("one", "two"):
            block = sw.generated_card_block(
                Path(self.card_path),
                f"org/{repo}",
                mockup=html,
                levels=sw.fidelity_levels(["layout=strict", "design=free"]),
            )
            self.assertIn(block, out)

    def test_a_zip_mockup_is_extracted_and_the_brief_names_the_folder(self):
        zpath = Path(self._tmp.name) / "poc.zip"
        _zip(zpath, {"index.html": "x"})
        self.hook_stdout["--card-mockup"] = f"{zpath}\n"
        _, out, _ = self._run("0099", "my-card", "org/one")
        folder = Path(self._tmp.name) / "poc"
        self.assertTrue((folder / "index.html").is_file())
        self.assertIn(f": {folder}\n", out)

    def test_a_tilde_path_is_expanded(self):
        with mock.patch.dict(os.environ, {"HOME": self._tmp.name}):
            (Path(self._tmp.name) / "m").mkdir()
            self.hook_stdout["--card-mockup"] = "~/m\n"
            _, out, _ = self._run("0099", "my-card", "org/one")
        self.assertIn(f": {self._tmp.name}/m\n", out)

    def test_a_missing_mockup_aborts(self):
        self.hook_stdout["--card-mockup"] = f"{self._tmp.name}/absent\n"
        with self.assertRaises(SystemExit) as cm:
            self._run("0099", "my-card")
        self.assertIn("absent", str(cm.exception))

    def test_cmux_dry_run_is_unchanged_by_a_declared_mockup(self):
        _, without, _ = self._run("--cmux", "--dry-run", "0099", "my-card")
        zpath = Path(self._tmp.name) / "poc.zip"
        _zip(zpath, {"index.html": "x"})
        self.hook_stdout["--card-mockup"] = f"{zpath}\n"
        _, with_mockup, _ = self._run("--cmux", "--dry-run", "0099", "my-card")
        self.assertEqual(with_mockup, without)
        self.assertFalse((Path(self._tmp.name) / "poc").exists())

    def test_a_card_without_a_mockup_prints_no_mockup_line(self):
        _, out, _ = self._run("0099", "my-card")
        self.assertNotIn("Mockup", out)


class TestRealReaderFeedsFidelityLevels(unittest.TestCase):
    """The real `stop_handoff.py` CLI (not the faked `_hook`) feeding the script, per card shape."""

    def _read(self, frontmatter: str) -> tuple[str, dict[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "c.md"
            card.write_text(f"---\n{frontmatter}---\n# c\n", encoding="utf-8")
            mockup = sw._hook("--card-mockup", str(card), allow_empty=True)
            items = sw._hook("--card-mockup-fidelity", str(card), allow_empty=True)
        return mockup, sw.fidelity_levels(items.splitlines())

    def test_spaces_around_the_equals_sign_reach_the_levels(self):
        _, levels = self._read("mockup: /m\nmockup_fidelity: [layout = strict]\n")
        self.assertEqual(levels["layout"], "strict")

    def test_an_unbracketed_list_is_not_read(self):
        _, levels = self._read("mockup: /m\nmockup_fidelity: layout=strict\n")
        self.assertEqual(levels, sw.fidelity_levels([]))

    def test_a_null_with_a_comment_is_no_mockup(self):
        mockup, _ = self._read("mockup: null # none yet\n")
        self.assertEqual(mockup, "")


if __name__ == "__main__":
    unittest.main()
