#!/usr/bin/env python3
"""Tests for plugins/pc-core/mcp/model_adversarial.py — run with:
pytest tests/test_model_adversarial.py

Never `python3 -m unittest discover tests`: it does not load `conftest.py`, so the autouse
fixture isolating `run_pool`'s debug log (`_isolate_pool_log`) never applies, and a real
`~/.claude/debug/` file gets written under the developer's own session id.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import model_adversarial as ma

MISTRAL = {
    "name": "mistral-medium-3.5",
    "kind": "cli",
    "provider": "mistral",
    "model": "mistral-medium-3.5",
    "capacity_tier": "opus",
    "free_tier": False,
    "concurrency": None,
}
GLM = {
    "name": "glm-5-2",
    "kind": "cli",
    "provider": "openrouter",
    "model": "z-ai/glm-5-2",
    "capacity_tier": "opus",
    "free_tier": True,
    "concurrency": None,
}
KIMI = {
    "name": "kimi-k2.6",
    "kind": "cli",
    "provider": "openrouter",
    "model": "moonshotai/kimi-k2.6:free",
    "capacity_tier": "sonnet",
    "free_tier": True,
    "concurrency": None,
}
OFFLINE = {
    "name": "local-ollama",
    "kind": "offline",
    "provider": "ollama",
    "model": None,
    "capacity_tier": "haiku",
    "free_tier": True,
    "concurrency": 1,
}
LOCAL_MLX = {
    "name": "local-qwen3.5",
    "kind": "cli",
    "provider": "mlx",
    "model": "mlx-community/Qwen3.5-9B-MLX-4bit",
    "capacity_tier": "haiku",
    "free_tier": True,
    "concurrency": 1,
    "local": True,
}
CLOUD_HAIKU = {
    "name": "openrouter-free",
    "kind": "cli",
    "provider": "openrouter",
    "model": "free",
    "capacity_tier": "haiku",
    "free_tier": True,
    "concurrency": None,
}
POOL = [MISTRAL, GLM, KIMI, OFFLINE]


class TestSelectForAdversarial(unittest.TestCase):
    def test_excludes_offline_and_orders_direct_before_aggregator(self):
        self.assertEqual(
            ma.select_for_adversarial(POOL, floor_tier="sonnet"), [MISTRAL, GLM, KIMI]
        )

    def test_floor_excludes_weaker_tiers(self):
        self.assertEqual(
            ma.select_for_adversarial(POOL, floor_tier="opus"), [MISTRAL, GLM]
        )

    def test_empty_pool_yields_empty_list(self):
        self.assertEqual(ma.select_for_adversarial([]), [])

    def test_local_sorts_before_direct_and_aggregator(self):
        # File order puts the cloud entries first — `local` must win regardless.
        pool = [MISTRAL, KIMI, LOCAL_MLX]
        self.assertEqual(
            ma.select_for_adversarial(pool, floor_tier="haiku"),
            [LOCAL_MLX, MISTRAL, KIMI],
        )


class TestSelectForDelegation(unittest.TestCase):
    def test_free_before_paid_then_cheapest_capable_tier(self):
        self.assertEqual(
            ma.select_for_delegation(POOL, task_tier="haiku"),
            [OFFLINE, KIMI, GLM, MISTRAL],
        )

    def test_floor_excludes_offline_for_a_sonnet_task(self):
        self.assertEqual(
            ma.select_for_delegation(POOL, task_tier="sonnet"), [KIMI, GLM, MISTRAL]
        )

    def test_local_beats_cloud_at_equal_tier_and_free_tier(self):
        # File order puts the cloud entry first — `local` must still come first in the result.
        pool = [CLOUD_HAIKU, LOCAL_MLX]
        self.assertEqual(
            ma.select_for_delegation(pool, task_tier="haiku"), [LOCAL_MLX, CLOUD_HAIKU]
        )

    def test_tier_rank_still_wins_over_locality(self):
        # A local entry at a higher tier never jumps ahead of a cheaper cloud entry —
        # "just capable enough" (tier_rank) stays the primary criterion, `local` only
        # tie-breaks within the same (free_tier, tier_rank) group.
        local_sonnet = {**LOCAL_MLX, "capacity_tier": "sonnet"}
        self.assertEqual(
            ma.select_for_delegation([local_sonnet, CLOUD_HAIKU], task_tier="haiku"),
            [CLOUD_HAIKU, local_sonnet],
        )


class TestSelectForDelegationWithFallback(unittest.TestCase):
    """A diff-derived `opus` request must not go BLOCKED just because the configured pool
    tops out at `sonnet` — confirmed live: every pool entry in this workspace's own
    `settings.pinky.json` is `sonnet` or below, so a large/security-sensitive diff always
    resolved to zero candidates before this fallback existed."""

    def test_no_fallback_needed_when_the_exact_tier_is_present(self):
        self.assertEqual(
            ma.select_for_delegation_with_fallback([MISTRAL, KIMI], "opus"), [MISTRAL]
        )

    def test_falls_back_to_the_next_tier_when_the_pool_tops_out_below_it(self):
        self.assertEqual(ma.select_for_delegation_with_fallback([KIMI], "opus"), [KIMI])

    def test_falls_all_the_way_to_haiku_when_nothing_higher_exists(self):
        self.assertEqual(
            ma.select_for_delegation_with_fallback([CLOUD_HAIKU], "opus"),
            [CLOUD_HAIKU],
        )

    def test_offline_entries_are_excluded_even_after_falling_back(self):
        offline_sonnet = {**OFFLINE, "capacity_tier": "sonnet"}
        self.assertEqual(
            ma.select_for_delegation_with_fallback([offline_sonnet], "opus"), []
        )

    def test_empty_pool_yields_empty_list(self):
        self.assertEqual(ma.select_for_delegation_with_fallback([], "opus"), [])


class TestLoadPool(unittest.TestCase):
    """Exercises the real `policy_lib` ascending walk — no mock of the resolver itself."""

    def setUp(self):
        import os

        self._tmp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self._tmp.name)
        self._original_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project_dir)

    def tearDown(self):
        import os

        if self._original_project_dir is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._original_project_dir
        self._tmp.cleanup()

    def test_no_settings_pinky_json_anywhere_returns_builtin_default(self):
        self.assertEqual(ma.load_pool(), ma._DEFAULT_POOL)

    def test_model_pool_key_from_settings_pinky_json_is_returned(self):
        custom_pool = [{"name": "custom", "kind": "cli", "capacity_tier": "haiku"}]
        (self.project_dir / ".claude").mkdir()
        (self.project_dir / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"model_pool": custom_pool})
        )
        self.assertEqual(ma.load_pool(), custom_pool)


class TestRunPool(unittest.TestCase):
    def test_offline_entry_is_never_called(self):
        # seulement une entrée offline : échec sans jamais appeler pi
        result = ma.run_pool([OFFLINE], "prompt")
        self.assertIsNone(result.entry)
        self.assertIn("offline entries are not callable", result.result)

    def test_http_entry_is_refused_with_how_to_migrate(self):
        http_entry = {**MISTRAL, "kind": "http"}
        result = ma.run_pool([http_entry], "prompt")
        self.assertIsNone(result.entry)
        self.assertEqual(
            result.result,
            "mistral-medium-3.5: kind 'http' is no longer supported — declare the provider "
            "in pi (~/.pi/agent/models.json) and set kind 'cli'",
        )


class TestRunPoolRequeue(unittest.TestCase):
    """Empty answer / timeout are transient: requeued once, at the end of the queue; a second
    such failure from the same entry drops it for good. Rate-limit/OOM/insufficient-credit stay
    definitive — never requeued."""

    def _empty(self):
        return subprocess.CompletedProcess([], 0, "   \n", "")

    def _ok(self, text="answer"):
        return subprocess.CompletedProcess([], 0, text, "")

    def test_empty_answer_then_success_on_requeue(self):
        with unittest.mock.patch(
            "subprocess.run", side_effect=[self._empty(), self._ok("answer")]
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(MISTRAL, "answer"))

    def test_timeout_then_success_on_requeue(self):
        with unittest.mock.patch(
            "subprocess.run",
            side_effect=[subprocess.TimeoutExpired("pi", 180), self._ok("answer")],
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(MISTRAL, "answer"))

    def test_timeout_marker_in_a_process_error_is_transient_too(self):
        timeout_error = subprocess.CalledProcessError(1, "pi", "", "504 timed out")
        with unittest.mock.patch(
            "subprocess.run", side_effect=[timeout_error, self._ok("answer")]
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(MISTRAL, "answer"))

    def test_second_failure_after_requeue_drops_the_entry_for_good(self):
        with unittest.mock.patch(
            "subprocess.run", side_effect=[self._empty(), self._empty()]
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)
        self.assertEqual(result.result, "mistral-medium-3.5: empty answer")

    def test_requeue_order_is_end_of_queue_not_in_place(self):
        # Deux entrées toujours vides : la file doit suivre A, B, A, B — jamais en place (A, A,
        # B, B) — avant de s'épuiser au bout de 2*N tentatives.
        with unittest.mock.patch(
            "subprocess.run", side_effect=[self._empty()] * 4
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(
            [call.args[0][3] for call in run.call_args_list],
            [
                "mistral/mistral-medium-3.5",
                "openrouter/z-ai/glm-5-2",
                "mistral/mistral-medium-3.5",
                "openrouter/z-ai/glm-5-2",
            ],
        )
        self.assertIsNone(result.entry)

    def test_rate_limited_entry_is_never_requeued(self):
        rate_limited = subprocess.CalledProcessError(1, "pi", "", "429 too many")
        with unittest.mock.patch(
            "subprocess.run", side_effect=[rate_limited, rate_limited]
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)

    def test_out_of_memory_entry_is_never_requeued(self):
        oom = subprocess.CalledProcessError(1, "pi", "", "503 out_of_memory")
        with unittest.mock.patch("subprocess.run", side_effect=[oom, oom]) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)

    def test_insufficient_credit_entry_is_never_requeued(self):
        credit = subprocess.CalledProcessError(1, "pi", "", "402 payment required")
        with unittest.mock.patch("subprocess.run", side_effect=[credit, credit]) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)

    def test_definitive_marker_wins_over_an_ambiguous_timeout_marker(self):
        # Un 3e appel (au-delà des 2 valeurs de `side_effect`) lèverait `StopIteration` — la
        # preuve que MISTRAL n'a pas été requeue.
        ambiguous = subprocess.CalledProcessError(
            1, "pi", "", "429 too many, gateway timed out"
        )
        rate_limited = subprocess.CalledProcessError(1, "pi", "", "429 too many")
        with unittest.mock.patch(
            "subprocess.run", side_effect=[ambiguous, rate_limited]
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)


class _FixedDatetime(datetime):
    """A `datetime` subclass whose `.now()` always answers the same instant — freezes
    `log_pool_event`'s timestamp so a test can assert the whole log line, not a fragment."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 1, tzinfo=tz)


class TestPoolLog(unittest.TestCase):
    def test_build_pool_log_line_is_pinned(self):
        when = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            ma.build_pool_log_line(when, "mistral-medium-3.5", "empty answer"),
            "2026-09-27T12:00:00+00:00 mistral-medium-3.5 empty answer\n",
        )


def test_log_pool_event_writes_the_session_scoped_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc123")
    (tmp_path / ".claude" / "debug").mkdir(parents=True)
    monkeypatch.setattr(ma, "datetime", _FixedDatetime)

    ma.log_pool_event("mistral-medium-3.5", "timeout")

    log_path = tmp_path / ".claude" / "debug" / "abc123_mcp__pc-core__model-pool.txt"
    assert log_path.read_text(encoding="utf-8") == (
        "2026-01-01T00:00:00+00:00 mistral-medium-3.5 timeout\n"
    )


def test_log_pool_event_with_no_session_id_falls_back_to_standalone(
    tmp_path, monkeypatch
):
    (tmp_path / ".claude" / "debug").mkdir(parents=True)
    monkeypatch.setattr(ma, "datetime", _FixedDatetime)

    ma.log_pool_event("mistral-medium-3.5", "timeout")

    log_path = (
        tmp_path / ".claude" / "debug" / "standalone_mcp__pc-core__model-pool.txt"
    )
    assert log_path.exists()


def test_log_pool_event_swallows_an_io_error(tmp_path, monkeypatch):
    # Le chemin de log cible est lui-même un répertoire : `open("a")` lève `IsADirectoryError`,
    # jamais laissée remonter.
    debug_dir = tmp_path / ".claude" / "debug"
    debug_dir.mkdir(parents=True)
    (debug_dir / "standalone_mcp__pc-core__model-pool.txt").mkdir()
    ma.log_pool_event("mistral-medium-3.5", "timeout")


def test_transient_failure_with_a_broken_log_path_still_returns_success(
    tmp_path, monkeypatch
):
    # Le chemin de log est lui-même un répertoire : `open("a")` lève `IsADirectoryError` (une
    # sous-classe d'OSError), avalée en silence — un run par ailleurs réussi ne doit jamais être
    # cassé par une écriture de log qui échoue.
    debug_dir = tmp_path / ".claude" / "debug"
    debug_dir.mkdir(parents=True)
    (debug_dir / "standalone_mcp__pc-core__model-pool.txt").mkdir()
    empty = subprocess.CompletedProcess([], 0, "   ", "")
    ok = subprocess.CompletedProcess([], 0, "answer", "")
    with unittest.mock.patch("subprocess.run", side_effect=[empty, ok]) as run:
        result = ma.run_pool([MISTRAL], "p")
    assert run.call_count == 2
    assert result == ma.PoolResult(MISTRAL, "answer")


def test_pool_exhaustion_logs_every_failure_and_the_final_event(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess1")
    (tmp_path / ".claude" / "debug").mkdir(parents=True)
    monkeypatch.setattr(ma, "datetime", _FixedDatetime)
    empty = subprocess.CompletedProcess([], 0, "", "")
    with unittest.mock.patch("subprocess.run", side_effect=[empty, empty]):
        ma.run_pool([MISTRAL], "p")
    log_path = tmp_path / ".claude" / "debug" / "sess1_mcp__pc-core__model-pool.txt"
    assert log_path.read_text(encoding="utf-8") == (
        "2026-01-01T00:00:00+00:00 mistral-medium-3.5 empty answer\n"
        "2026-01-01T00:00:00+00:00 mistral-medium-3.5 empty answer\n"
        "2026-01-01T00:00:00+00:00 (pool) exhausted\n"
    )


class TestCallCli(unittest.TestCase):
    def _ok(self):
        return subprocess.CompletedProcess([], 0, "answer\n", "")

    def test_default_argv_has_no_tools_and_closes_stdin(self):
        with unittest.mock.patch("subprocess.run", return_value=self._ok()) as run:
            self.assertEqual(ma.call_cli(MISTRAL, "p", 30), "answer\n")
        run.assert_called_once_with(
            [
                "pi",
                "-p",
                "--model",
                "mistral/mistral-medium-3.5",
                "--no-tools",
                "--no-session",
                "--",
                "p",
            ],
            stdin=subprocess.DEVNULL,
            env=unittest.mock.ANY,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    def test_entry_thinking_is_passed_before_the_prompt(self):
        with unittest.mock.patch("subprocess.run", return_value=self._ok()) as run:
            ma.call_cli({**MISTRAL, "thinking": "off"}, "p", 30)
        self.assertEqual(run.call_args[0][0][-4:], ["--thinking", "off", "--", "p"])

    def test_no_entry_thinking_sends_no_thinking_flag(self):
        with unittest.mock.patch("subprocess.run", return_value=self._ok()) as run:
            ma.call_cli(MISTRAL, "p", 30)
        self.assertNotIn("--thinking", run.call_args[0][0])

    def test_tools_become_a_comma_separated_allowlist(self):
        with unittest.mock.patch("subprocess.run", return_value=self._ok()) as run:
            ma.call_cli(MISTRAL, "p", 30, ["read", "grep"])
        self.assertEqual(run.call_args[0][0][4:6], ["-t", "read,grep"])

    def test_given_env_is_layered_over_the_process_env(self):
        with unittest.mock.patch("subprocess.run", return_value=self._ok()) as run:
            ma.call_cli(MISTRAL, "p", 30, env={"PINKY_TEST_VAR": "layered"})
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PINKY_TEST_VAR"], "layered")
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_without_env_it_reads_secret_env(self):
        with (
            unittest.mock.patch.object(ma, "secret_env", return_value={"K": "v"}),
            unittest.mock.patch("subprocess.run", return_value=self._ok()) as run,
        ):
            ma.call_cli(MISTRAL, "p", 30)
        self.assertEqual(run.call_args.kwargs["env"]["K"], "v")

    def test_non_cli_entry_raises(self):
        with self.assertRaises(ValueError):
            ma.call_cli({**MISTRAL, "kind": "http"}, "p", 30)


class TestSecretsAreReadOncePerRun(unittest.TestCase):
    def test_run_pool_reads_the_keychain_once_for_every_entry_it_tries(self):
        rate_limited = subprocess.CalledProcessError(1, "pi", "", "429 too many")
        ok = subprocess.CompletedProcess([], 0, "answer", "")
        with (
            unittest.mock.patch.object(
                ma, "secret_env", return_value={"K": "v"}
            ) as secrets,
            unittest.mock.patch(
                "subprocess.run", side_effect=[rate_limited, ok]
            ) as run,
        ):
            ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(secrets.call_count, 1)
        self.assertEqual([c.kwargs["env"]["K"] for c in run.call_args_list], ["v", "v"])

    def test_check_pool_reads_the_keychain_once(self):
        ok = subprocess.CompletedProcess([], 0, "OK", "")
        with (
            unittest.mock.patch.object(ma, "secret_env", return_value={}) as secrets,
            unittest.mock.patch("subprocess.run", return_value=ok),
        ):
            ma.check_pool([MISTRAL, GLM], timeout=5)
        self.assertEqual(secrets.call_count, 1)


class TestEntryTimeout(unittest.TestCase):
    def test_entry_timeout_overrides_the_call_default(self):
        self.assertEqual(ma.entry_timeout({**MISTRAL, "timeout": 300}, 180), 300)

    def test_missing_entry_timeout_keeps_the_call_default(self):
        self.assertEqual(ma.entry_timeout(MISTRAL, 180), 180)

    def test_run_pool_gives_each_entry_its_own_timeout(self):
        ok = subprocess.CompletedProcess([], 0, "answer", "")
        slow = {**GLM, "timeout": 300}
        with unittest.mock.patch("subprocess.run", return_value=ok) as run:
            ma.run_pool([slow], "p", timeout=180)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)

    def test_check_pool_waits_the_entry_timeout(self):
        with unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("pi", 300)
        ) as run:
            (result,) = ma.check_pool([{**GLM, "timeout": 300}], timeout=60)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        self.assertEqual(result.detail, "no answer within 300s")


class TestFilterPool(unittest.TestCase):
    def test_no_filter_keeps_the_whole_pool(self):
        self.assertEqual(ma.filter_pool(POOL), POOL)

    def test_provider_keeps_only_its_entries(self):
        self.assertEqual(ma.filter_pool(POOL, provider="openrouter"), [GLM, KIMI])

    def test_names_keep_only_those_entries_in_pool_order(self):
        self.assertEqual(
            ma.filter_pool(POOL, names=["kimi-k2.6", "mistral-medium-3.5"]),
            [MISTRAL, KIMI],
        )

    def test_provider_and_names_both_apply(self):
        self.assertEqual(
            ma.filter_pool(POOL, provider="openrouter", names=["mistral-medium-3.5"]),
            [],
        )


def test_check_pool_cli_says_when_a_filter_matches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(ma, "load_pool", lambda: POOL)
    assert ma.main(["--check-pool", "--provider", "nvidai"]) == 2
    assert capsys.readouterr().err == (
        "[pool] no entry matches provider 'nvidai' — check the spelling against model_pool\n"
    )


def test_check_pool_cli_checks_only_the_filtered_entries(monkeypatch, capsys):
    seen: list[str] = []

    def fake_check_pool(pool, timeout=60, workers=ma.CHECK_WORKERS):
        seen.extend(e["name"] for e in pool)
        return [ma.PoolCheck(e["name"], "ok", "OK", 1.0) for e in pool]

    monkeypatch.setattr(ma, "load_pool", lambda: POOL)
    monkeypatch.setattr(ma, "check_pool", fake_check_pool)
    assert ma.main(["--check-pool", "--name", "glm-5-2"]) == 0
    assert seen == ["glm-5-2"]


class TestCheckPool(unittest.TestCase):
    def test_one_result_per_entry_offline_skipped(self):
        def fake_run(cmd, **kw):
            if cmd[3] == "openrouter/z-ai/glm-5-2":
                raise subprocess.CalledProcessError(1, cmd, "", "401 unauthorized\n")
            return subprocess.CompletedProcess(cmd, 0, "OK\n", "")

        with unittest.mock.patch("subprocess.run", side_effect=fake_run):
            results = ma.check_pool([MISTRAL, GLM, OFFLINE], timeout=5)

        self.assertEqual(
            [(r.name, r.status, r.detail) for r in results],
            [
                ("mistral-medium-3.5", "ok", "OK"),
                ("glm-5-2", "failed", "401 unauthorized"),
                ("local-ollama", "skipped", "offline — checked by `pinky doctor`"),
            ],
        )

    def test_timeout_is_a_failure(self):
        with unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("pi", 5)
        ):
            (result,) = ma.check_pool([MISTRAL], timeout=5)
        self.assertEqual(
            (result.status, result.detail), ("failed", "no answer within 5s")
        )

    def test_render_pool_check(self):
        results = [
            ma.PoolCheck("mistral-medium-3.5", "ok", "OK", 1.24),
            ma.PoolCheck("glm-5-2", "failed", "401 unauthorized", 0.5),
            ma.PoolCheck(
                "local-ollama", "skipped", "offline — checked by `pinky doctor`", 0.0
            ),
        ]
        self.assertEqual(
            ma.render_pool_check(results),
            "[pool] 1/2 answered\n"
            "✅ mistral-medium-3.5  1.2s\n"
            "❌ glm-5-2             401 unauthorized\n"
            "•  local-ollama        offline — checked by `pinky doctor`",
        )


class TestLoadMethodology(unittest.TestCase):
    def test_strips_frontmatter(self):
        text = ma.load_methodology()
        self.assertFalse(text.startswith("---"))
        self.assertNotIn("name: review-guidelines", text)

    def test_contains_output_format_section(self):
        # the one section every consumer (agent + this tool) must share, so findings from either
        # path look the same regardless of which model produced them
        self.assertIn("## Output format", ma.load_methodology())

    def test_missing_file_returns_empty_not_raises(self):
        original = ma._METHODOLOGY_PATH
        ma._METHODOLOGY_PATH = Path("/nonexistent/reviewer.md")
        try:
            self.assertEqual(ma.load_methodology(), "")
        finally:
            ma._METHODOLOGY_PATH = original


class TestStackReview(unittest.TestCase):
    def test_known_stack_appends_its_checklist(self):
        base = ma.load_methodology()
        withfastapi = ma.load_methodology("fastapi")
        self.assertTrue(withfastapi.startswith(base))
        self.assertGreater(len(withfastapi), len(base))
        self.assertIn("FastAPI", withfastapi)

    def test_unknown_stack_changes_nothing(self):
        self.assertEqual(ma.load_methodology("cobol"), ma.load_methodology())

    def test_uninstalled_plugin_degrades_to_no_section(self):
        # the file simply does not resolve — same graceful-degrade as a missing methodology file
        original = ma._STACK_SKILLS["fastapi"]
        ma._STACK_SKILLS["fastapi"] = ("pc-not-installed", "nope")
        try:
            self.assertEqual(ma.load_methodology("fastapi"), ma.load_methodology())
        finally:
            ma._STACK_SKILLS["fastapi"] = original

    def test_stack_review_path_none_for_unknown_key(self):
        self.assertIsNone(ma._stack_review_path("cobol"))


class TestDetectStack(unittest.TestCase):
    def _write(self, text: str) -> str:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        f.write(text)
        f.close()
        return f.name

    def test_fastapi_import_is_detected(self):
        self.assertEqual(
            ma.detect_stack([self._write("from fastapi import APIRouter\n")]), "fastapi"
        )

    def test_streamlit_import_is_detected(self):
        self.assertEqual(
            ma.detect_stack([self._write("import streamlit as st\n")]), "streamlit"
        )

    def test_no_marker_returns_none(self):
        self.assertIsNone(ma.detect_stack([self._write("import os\nx = 1\n")]))

    def test_a_bare_mention_is_not_a_match(self):
        # "fastapi" in a comment or string must not trigger — the marker is anchored on an import
        src = '# this is not a fastapi app\nMSG = "see fastapi docs"\nimport os\n'
        self.assertIsNone(ma.detect_stack([self._write(src)]))

    def test_unreadable_file_is_skipped_not_raised(self):
        self.assertIsNone(ma.detect_stack(["/nonexistent/whatever.py"]))


def test_adversarial_tool_folds_stack_checklist_into_the_prompt(monkeypatch, tmp_path):
    """The pool model has no `Skill` tool, so the stack checklist must be in the prompt."""
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp")
    )
    import server

    src = tmp_path / "api.py"
    src.write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n", encoding="utf-8"
    )

    captured: dict[str, str] = {}

    def fake_run_pool(entries, prompt, timeout=180, tools=None):
        captured["prompt"] = prompt
        return ma.PoolResult(entries[0], "ok")

    monkeypatch.setattr(server.model_adversarial, "run_pool", fake_run_pool)

    out = server.adversarial(task="review this", files=[str(src)])
    # a phrase unique to the FastAPI review reference, not the generic methodology
    assert "N+1 query pattern" in captured["prompt"]
    assert out.startswith("[answered by ")


def test_adversarial_tool_ignores_an_unknown_tier(monkeypatch):
    """An unrecognised tier must not route — fall back to the default adversarial selection."""
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp")
    )
    import server

    seen: dict[str, list] = {}

    def fake_select_for_adversarial(pool, floor_tier="sonnet"):
        seen["adversarial"] = pool
        return [MISTRAL]

    def fake_select_for_delegation(pool, task_tier="haiku"):
        seen["delegation"] = task_tier
        return [MISTRAL]

    monkeypatch.setattr(
        server.model_adversarial,
        "select_for_adversarial",
        fake_select_for_adversarial,
    )
    monkeypatch.setattr(
        server.model_adversarial,
        "select_for_delegation",
        fake_select_for_delegation,
    )
    monkeypatch.setattr(
        server.model_adversarial,
        "run_pool",
        lambda entries, prompt, timeout=180, tools=None: ma.PoolResult(
            entries[0], "ok"
        ),
    )

    server.adversarial(task="review", tier="turbo-9000")
    assert "adversarial" in seen and "delegation" not in seen


def test_adversarial_tool_reports_pool_exhaustion_without_raising(monkeypatch):
    """`run_pool` returning `entry=None` (a fully exhausted pool) must read as a BLOCKED report,
    never a `TypeError` on `entry['name']`."""
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp")
    )
    import pc_core_server

    monkeypatch.setattr(
        pc_core_server.model_adversarial,
        "run_pool",
        lambda entries, prompt, timeout=180, tools=None: ma.PoolResult(
            None, "everything failed"
        ),
    )

    # `model="x"` bypasses `load_pool` via the override path, straight to `run_pool`.
    out = pc_core_server.adversarial(task="review", model="x")
    assert (
        out == "[adversarial] BLOCKED — every candidate failed. Last: everything failed"
    )


if __name__ == "__main__":
    unittest.main()
