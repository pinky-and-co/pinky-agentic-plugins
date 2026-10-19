#!/usr/bin/env python3
"""Tests for plugins/pc-core/mcp/model_adversarial.py — run with:
pytest tests/test_model_adversarial.py

Never `python3 -m unittest discover tests`: it does not load `conftest.py`, so the autouse
fixture isolating `run_pool`'s debug log (`_isolate_pool_log`) never applies, and a real
`~/.claude/debug/` file gets written under the developer's own session id.
"""

import concurrent.futures
import contextlib
import inspect
import io
import json
import os
import random
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import call_scope
import model_adversarial as ma
import pi_events_view as pev
from pc_core_stdio import PoolServer, alive
from pi_fakes import PiPopen, pi_events

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


def _entry(name, provider, tier="sonnet", **extra):
    return {
        "name": name,
        "kind": "cli",
        "provider": provider,
        "model": name,
        "capacity_tier": tier,
        "free_tier": True,
        "concurrency": None,
        **extra,
    }


NV_GLM = _entry("nvidia-glm", "nvidia", "opus")
NV_FLASH = _entry("nvidia-flash", "nvidia", "sonnet")
NV_KIMI = _entry("nvidia-kimi", "nvidia", "opus")
VC_A = _entry("vercel-a", "vercel-ai-gateway")
VC_B = _entry("vercel-b", "vercel-ai-gateway")
RQ_A = _entry("requesty-a", "requesty")


class TestSelectForAdversarial(unittest.TestCase):
    def test_excludes_offline_and_orders_direct_before_aggregator(self):
        self.assertEqual(
            ma.select_for_adversarial(POOL, floor_tier="sonnet"), [MISTRAL, GLM, KIMI]
        )

    def test_floor_excludes_haiku_entries(self):
        self.assertEqual(
            ma.select_for_adversarial([MISTRAL, CLOUD_HAIKU], floor_tier="sonnet"),
            [MISTRAL],
        )

    def test_sonnet_and_opus_are_equivalent_for_the_floor(self):
        self.assertEqual(
            ma.select_for_adversarial(POOL, floor_tier="opus"), [MISTRAL, GLM, KIMI]
        )

    def test_never_two_entries_of_one_provider_back_to_back(self):
        pool = [NV_GLM, NV_FLASH, NV_KIMI, VC_A, VC_B, RQ_A]
        self.assertEqual(
            ma.select_for_adversarial(pool),
            [NV_GLM, VC_A, RQ_A, NV_FLASH, VC_B, NV_KIMI],
        )

    def test_spreading_never_promotes_an_aggregator_over_a_direct_provider(self):
        openrouter = _entry("or-a", "openrouter")
        pool = [openrouter, NV_GLM, NV_KIMI]
        self.assertEqual(ma.select_for_adversarial(pool), [NV_GLM, NV_KIMI, openrouter])

    def test_empty_pool_yields_empty_list(self):
        self.assertEqual(ma.select_for_adversarial([]), [])

    def test_local_sorts_before_direct_and_aggregator(self):
        # File order puts the cloud entries first — `local` must win regardless.
        pool = [MISTRAL, KIMI, LOCAL_MLX]
        self.assertEqual(
            ma.select_for_adversarial(pool, floor_tier="haiku"),
            [LOCAL_MLX, MISTRAL, KIMI],
        )


class TestProviderPriority(unittest.TestCase):
    """`priority` (higher first, default 0) orders the providers inside a round of the spread —
    a generous provider opens every round, without stacking its entries back to back."""

    def test_the_prioritised_provider_opens_each_round(self):
        ol_a = _entry("ollama-a", "ollama-cloud", priority=1)
        ol_b = _entry("ollama-b", "ollama-cloud", priority=1)
        pool = [NV_GLM, NV_KIMI, VC_A, ol_a, ol_b]
        self.assertEqual(
            ma.select_for_adversarial(pool),
            [ol_a, NV_GLM, VC_A, ol_b, NV_KIMI],
        )

    def test_delegation_honours_priority_too(self):
        ol_a = _entry("ollama-a", "ollama-cloud", priority=1)
        pool = [NV_GLM, VC_A, ol_a]
        self.assertEqual(
            ma.select_for_delegation(pool, task_tier="sonnet"),
            [ol_a, NV_GLM, VC_A],
        )

    def test_priority_never_outranks_locality_or_cost(self):
        paid_ollama = _entry("ollama-paid", "ollama-cloud", priority=9, free_tier=False)
        pool = [paid_ollama, NV_GLM]
        self.assertEqual(
            ma.select_for_delegation(pool, task_tier="sonnet"), [NV_GLM, paid_ollama]
        )


class TestAdversarialRandomTie(unittest.TestCase):
    """Between entries of the same `priority` the adversarial order is drawn; `priority` stays a
    strict order between levels. Each draw uses its own seeded rng so the test is reproducible."""

    @staticmethod
    def _heads(pool, draws=40):
        return [
            ma.select_for_adversarial(pool, rng=random.Random(seed))[0]["name"]
            for seed in range(draws)
        ]

    def test_equal_priority_entries_of_one_provider_vary_their_head(self):
        pool = [
            _entry("ol-a", "ollama-cloud", priority=1),
            _entry("ol-b", "ollama-cloud", priority=1),
            _entry("ol-c", "ollama-cloud", priority=1),
        ]
        self.assertEqual(set(self._heads(pool)), {"ol-a", "ol-b", "ol-c"})

    def test_a_higher_priority_entry_opens_every_draw(self):
        pool = [
            _entry("ol-a", "ollama-cloud", priority=1),
            _entry("ol-b", "ollama-cloud", priority=1),
            _entry("ol-top", "ollama-cloud", priority=2),
        ]
        self.assertEqual(set(self._heads(pool)), {"ol-top"})

    def test_providers_with_equal_top_priority_swap_the_opening_slot(self):
        pool = [NV_GLM, NV_KIMI, VC_A]
        providers = {
            ma.select_for_adversarial(pool, rng=random.Random(seed))[0]["provider"]
            for seed in range(40)
        }
        self.assertEqual(providers, {"nvidia", "vercel-ai-gateway"})

    def test_without_rng_spread_providers_keeps_pool_order(self):
        pool = [NV_GLM, NV_KIMI, VC_A]
        self.assertEqual(
            ma.spread_providers(pool, key=lambda e: 0), [NV_GLM, VC_A, NV_KIMI]
        )


class TestAnswerLabel(unittest.TestCase):
    def test_provider_slash_last_model_segment_then_tier_badge(self):
        entry = _entry("nvidia-glm-5.3", "nvidia", "opus", model="z-ai/glm-5.3")
        self.assertEqual(ma.answer_label(entry), "nvidia/glm-5.3 · 🟧 opus")

    def test_each_tier_has_its_own_badge(self):
        labels = [
            ma.answer_label(_entry("x", "p", tier, model="m"))
            for tier in ("haiku", "sonnet", "opus")
        ]
        self.assertEqual(labels, ["p/m · 🟦 haiku", "p/m · 🟪 sonnet", "p/m · 🟧 opus"])

    def test_an_unknown_tier_is_shown_without_a_badge(self):
        entry = _entry("x", "p", "mystery", model="org/m:free")
        self.assertEqual(ma.answer_label(entry), "p/m:free · mystery")


class TestSelectForDelegation(unittest.TestCase):
    def test_free_before_paid_then_cheapest_capable_tier(self):
        self.assertEqual(
            ma.select_for_delegation(POOL, task_tier="haiku"),
            [OFFLINE, GLM, KIMI, MISTRAL],
        )

    def test_floor_excludes_offline_for_a_sonnet_task(self):
        self.assertEqual(
            ma.select_for_delegation(POOL, task_tier="sonnet"), [GLM, KIMI, MISTRAL]
        )

    def test_local_beats_cloud_at_equal_tier_and_free_tier(self):
        # File order puts the cloud entry first — `local` must still come first in the result.
        pool = [CLOUD_HAIKU, LOCAL_MLX]
        self.assertEqual(
            ma.select_for_delegation(pool, task_tier="haiku"), [LOCAL_MLX, CLOUD_HAIKU]
        )

    def test_never_two_entries_of_one_provider_back_to_back(self):
        pool = [NV_GLM, NV_FLASH, NV_KIMI, VC_A, VC_B]
        self.assertEqual(
            ma.select_for_delegation(pool, task_tier="sonnet"),
            [NV_GLM, VC_A, NV_FLASH, VC_B, NV_KIMI],
        )

    def test_spreading_stays_inside_a_free_tier_group(self):
        paid_vercel = {**VC_A, "free_tier": False}
        pool = [paid_vercel, NV_GLM, NV_KIMI]
        self.assertEqual(
            ma.select_for_delegation(pool, task_tier="sonnet"),
            [NV_GLM, NV_KIMI, paid_vercel],
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
    tops out at `sonnet`, nor be answered by the few entries labelled `opus` alone — sonnet and
    opus are one capacity class, so a provider that only lists `opus` entries cannot hold the
    whole queue."""

    def test_sonnet_entries_answer_an_opus_request(self):
        self.assertEqual(
            ma.select_for_delegation_with_fallback([MISTRAL, KIMI], "opus"),
            [KIMI, MISTRAL],
        )

    def test_an_opus_request_reaches_other_providers_before_a_third_entry_of_the_first(
        self,
    ):
        pool = [NV_GLM, NV_FLASH, NV_KIMI, VC_A]
        self.assertEqual(
            ma.select_for_delegation_with_fallback(pool, "opus"),
            [NV_GLM, VC_A, NV_FLASH, NV_KIMI],
        )

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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.py").write_text("one\n")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def test_diff_against_covers_commits_working_tree_and_untracked_files(repo):
    (repo / "a.py").write_text("one\ntwo\n")
    _git(repo, "commit", "-qam", "committed change")
    (repo / "a.py").write_text("one\ntwo\nthree\n")
    (repo / "new.py").write_text("fresh\n")

    diff = ma.diff_against("main", str(repo))

    assert diff == (
        "diff --git a/a.py b/a.py\n"
        "index 5626abf..4cb29ea 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,3 @@\n"
        " one\n"
        "+two\n"
        "+three\n"
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "index 0000000..92d5444\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+fresh\n"
    )


def test_diff_against_skips_an_oversized_untracked_file(repo, monkeypatch):
    monkeypatch.setattr(ma, "_MAX_UNTRACKED_BYTES", 10)
    (repo / "big.txt").write_text("x" * 11)
    (repo / "ok.txt").write_text("small\n")

    diff = ma.diff_against("main", str(repo))

    assert "untracked file skipped: big.txt (11 bytes > 10)\n" in diff
    assert "+small\n" in diff
    assert "+xxxxxxxxxxx" not in diff


@pytest.fixture
def mixed_repo(repo):
    _git(repo, "checkout", "-q", "main")
    (repo / "b.py").write_text("b1\n")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-q", "-m", "b")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--ff-only", "main")
    (repo / "a.py").write_text("one\ntwo\n")
    _git(repo, "add", "a.py")
    (repo / "b.py").write_text("b1\nb2\n")
    (repo / "new.py").write_text("fresh\n")
    return repo


def test_diff_against_staged_scope_is_the_index_only(mixed_repo):
    assert ma.diff_against("main", str(mixed_repo), scope="staged") == (
        "diff --git a/a.py b/a.py\n"
        "index 5626abf..814f4a4 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,2 @@\n"
        " one\n"
        "+two\n"
    )


def test_diff_against_unstaged_scope_is_the_worktree_plus_untracked(mixed_repo):
    assert ma.diff_against("main", str(mixed_repo), scope="unstaged") == (
        "diff --git a/b.py b/b.py\n"
        "index c9c6af7..9b89cd5 100644\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1 +1,2 @@\n"
        " b1\n"
        "+b2\n"
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "index 0000000..92d5444\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+fresh\n"
    )


def test_diff_against_all_scope_names_every_changed_file(mixed_repo):
    diff = ma.diff_against("main", str(mixed_repo))
    assert [line for line in diff.splitlines() if line.startswith("diff --git")] == [
        "diff --git a/a.py b/a.py",
        "diff --git a/b.py b/b.py",
        "diff --git a/new.py b/new.py",
    ]


def test_diff_against_refuses_an_unknown_scope(repo):
    with pytest.raises(ValueError):
        ma.diff_against("main", str(repo), scope="everything")


def test_repo_root_is_the_top_level_even_from_a_subdirectory(repo):
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    assert ma.repo_root(str(sub)) == str(repo.resolve())


def test_diff_against_from_a_subdirectory_covers_the_whole_repository(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "inner.py").write_text("inner\n")
    (repo / "top.py").write_text("top\n")

    diff = ma.diff_against("main", str(repo / "pkg"))

    assert [line for line in diff.splitlines() if line.startswith("diff --git")] == [
        "diff --git a/pkg/inner.py b/pkg/inner.py",
        "diff --git a/top.py b/top.py",
    ]


def test_diff_against_notes_an_untracked_entry_that_is_not_a_regular_file(repo):
    (repo / "dangling").symlink_to(repo / "missing")

    assert ma.diff_against("main", str(repo)) == (
        "# untracked entry skipped: dangling (not a regular file)\n"
    )


def test_diff_against_survives_a_file_that_is_not_utf8(repo):
    (repo / "a.py").write_bytes(b"caf\xe9\n")

    diff = ma.diff_against("main", str(repo))

    assert "+caf\ufffd\n" in diff


def test_diff_against_truncates_one_oversized_file_diff_and_keeps_the_others(
    repo, monkeypatch
):
    monkeypatch.setattr(ma, "_MAX_FILE_DIFF_CHARS", 130)
    (repo / "a.py").write_text("one\n" + "x\n" * 200)
    (repo / "b.py").write_text("small\n")
    _git(repo, "add", "b.py")

    assert ma.diff_against("main", str(repo)) == (
        "diff --git a/a.py b/a.py\n"
        "index 5626abf..481a234 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,201 @@\n"
        " one\n" + "+x\n" * 10 + "# diff truncated: a.py (698 chars > 130)\n"
        "diff --git a/b.py b/b.py\n"
        "new file mode 100644\n"
        "index 0000000..ac79041\n"
        "--- /dev/null\n"
        "+++ b/b.py\n"
        "@@ -0,0 +1 @@\n"
        "+small\n"
    )


def test_diff_against_is_empty_when_nothing_changed(repo):
    assert ma.diff_against("main", str(repo)) == ""


def test_diff_against_refuses_an_option_shaped_base(repo):
    with pytest.raises(ValueError):
        ma.diff_against("--output=/tmp/x", str(repo))


def test_diff_against_an_unknown_base_raises_the_git_error(repo):
    with pytest.raises(subprocess.CalledProcessError):
        ma.diff_against("no-such-branch", str(repo))


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
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[self._empty(), self._ok("answer")],
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(MISTRAL, "answer"))

    def test_tool_call_text_in_plural_form_requeues(self):
        # Un modèle sans outils écrit parfois les balises d'appel au pluriel
        # (`<tool_calls>`…`</tool_calls>`) — même signal, même requeue.
        text_call = (
            "I'll review the suite.\n"
            "<tool_calls>\n"
            '<invoke name="Bash">\n'
            '<parameter name="command">ls -la</parameter>\n'
            "</invoke>\n"
            "</tool_calls>"
        )
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[self._ok(text_call), self._ok("answer")],
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(MISTRAL, "answer"))

    def test_run_wall_clock_is_a_backstop_never_requeued(self):
        # Le chrono global coupe une boucle d'outils : la rejouer referait toute l'exploration.
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[subprocess.TimeoutExpired("pi", 900), self._ok("answer")],
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 1)
        self.assertIsNone(result.entry)
        self.assertEqual(
            result.result,
            "mistral-medium-3.5: Command 'pi' timed out after 900 seconds",
        )

    def test_run_wall_clock_falls_through_to_the_next_entry(self):
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[subprocess.TimeoutExpired("pi", 900), self._ok("answer")],
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(GLM, "answer"))

    def test_the_default_has_no_wall_clock(self):
        self.assertIsNone(ma.RUN_TIMEOUT)
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=self._ok()
        ) as run:
            ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.wait_timeouts, [None])

    def test_timeout_marker_in_a_process_error_is_transient_too(self):
        timeout_error = subprocess.CalledProcessError(1, "pi", "", "504 timed out")
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[timeout_error, self._ok("answer")],
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(MISTRAL, "answer"))

    def test_second_failure_after_requeue_drops_the_entry_for_good(self):
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[self._empty(), self._empty()],
        ) as run:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)
        self.assertEqual(result.result, "mistral-medium-3.5: empty answer")

    def test_answer_ending_on_a_closing_tool_call_tag_falls_through_to_the_next_entry(
        self,
    ):
        # Un modèle sans outils écrit son appel d'outil en texte : ce n'est pas un avis.
        for tail in ("</function>", "</tool_call>", "</function>\n  \n"):
            with self.subTest(tail=tail):
                answers = [f"<function=read>\n<parameter=path>a.py{tail}", "answer"]
                with unittest.mock.patch.object(
                    ma, "call_cli", side_effect=answers
                ) as call:
                    result = ma.run_pool([MISTRAL, GLM], "p")
                self.assertEqual(call.call_count, 2)
                self.assertEqual(result, ma.PoolResult(GLM, "answer"))

    def test_tool_call_text_takes_the_empty_answer_path_requeue_then_drop(self):
        text = "<function=grep>\n<parameter=pattern>x</parameter>\n</function>"
        with unittest.mock.patch.object(
            ma, "call_cli", side_effect=[text, text]
        ) as call:
            result = ma.run_pool([MISTRAL], "p")
        self.assertEqual(call.call_count, 2)
        self.assertIsNone(result.entry)

    def test_tool_call_markup_quoted_mid_text_with_prose_after_is_an_answer(self):
        review = (
            "The server parses <tool_call>read a.py</tool_call> blocks out of the text; "
            "that regex never matches nested tags.\nVerdict: approved."
        )
        with unittest.mock.patch.object(
            ma, "call_cli", side_effect=[review, "second"]
        ) as call:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result, ma.PoolResult(MISTRAL, review))

    def test_requeue_order_is_end_of_queue_not_in_place(self):
        # Deux entrées toujours vides : la file doit suivre A, B, A, B — jamais en place (A, A,
        # B, B) — avant de s'épuiser au bout de 2*N tentatives.
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[self._empty()] * 4,
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(
            [call.args[0][4] for call in run.call_args_list],
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
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[rate_limited, rate_limited],
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)

    def test_out_of_memory_entry_is_never_requeued(self):
        oom = subprocess.CalledProcessError(1, "pi", "", "503 out_of_memory")
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, side_effect=[oom, oom]
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)

    def test_insufficient_credit_entry_is_never_requeued(self):
        credit = subprocess.CalledProcessError(1, "pi", "", "402 payment required")
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[credit, credit],
        ) as run:
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
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[ambiguous, rate_limited],
        ) as run:
            result = ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(result.entry)


class TestRunPoolProviderLimit(unittest.TestCase):
    """A 429 / 402 is the provider's (quota, balance): its other queued entries are skipped.
    A timeout or an out-of-memory says nothing about the provider's other models."""

    def _ok(self):
        return subprocess.CompletedProcess([], 0, "answer", "")

    def test_a_model_level_429_keeps_the_same_providers_other_entries(self):
        # Formes réelles : Groq nomme le modèle (et l'organisation) dans une limite par modèle ;
        # vercel cite le modèle et sa propre limite d'équipe.
        groq = subprocess.CalledProcessError(
            1,
            "pi",
            "",
            "429: Rate limit reached for model `openai/gpt-oss-120b` in organization "
            "`org_1` on tokens per minute (TPM): Limit 8000, Used 5700, Requested 6209.",
        )
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[groq, self._ok()],
        ) as run:
            result = ma.run_pool([NV_GLM, NV_KIMI, VC_A], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(NV_KIMI, "answer"))

    def test_an_organization_level_429_drops_the_same_providers_remaining_entries(self):
        org = subprocess.CalledProcessError(
            1,
            "pi",
            "",
            '429: {"origin":"router","message":"Your organization reached its limit of '
            'concurrent requests."}',
        )
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[org, self._ok()],
        ) as run:
            result = ma.run_pool([NV_GLM, NV_KIMI, VC_A], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result, ma.PoolResult(VC_A, "answer"))

    def test_a_stray_402_earlier_in_the_output_does_not_condemn_the_provider(self):
        noisy = subprocess.CalledProcessError(
            1,
            "pi",
            "",
            "read 402 lines\n429: Rate limit reached for model `m` on tokens per minute",
        )
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[noisy, self._ok()],
        ) as run:
            result = ma.run_pool([NV_GLM, NV_KIMI], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result.entry, NV_KIMI)

    def test_the_skipped_entry_keeps_the_real_error_as_the_last_one(self):
        credit = subprocess.CalledProcessError(1, "pi", "", "402 payment required")
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, side_effect=[credit]
        ):
            result = ma.run_pool([NV_GLM, NV_KIMI], "p")
        self.assertIsNone(result.entry)
        self.assertEqual(result.result, "nvidia-glm: 402 payment required")

    def test_insufficient_credit_drops_the_provider_too(self):
        credit = subprocess.CalledProcessError(1, "pi", "", "402 payment required")
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[credit, self._ok()],
        ) as run:
            result = ma.run_pool([NV_GLM, NV_KIMI, VC_A], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result.entry, VC_A)

    def test_timeout_keeps_the_same_providers_other_entries(self):
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[subprocess.TimeoutExpired("pi", 1), self._ok()],
        ) as run:
            result = ma.run_pool([NV_GLM, NV_KIMI], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result.entry, NV_KIMI)

    def test_out_of_memory_keeps_the_same_providers_other_entries(self):
        oom = subprocess.CalledProcessError(1, "pi", "", "503 out_of_memory")
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=[oom, self._ok()],
        ) as run:
            result = ma.run_pool([NV_GLM, NV_KIMI], "p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result.entry, NV_KIMI)


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

    def test_build_pool_log_line_appends_fields_in_order(self):
        when = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            ma.build_pool_log_line(
                when,
                "nvidia-glm",
                "timeout",
                {
                    "model": "nvidia/z-ai/glm-5.3",
                    "prompt_chars": 48213,
                    "timeout": "300s",
                },
            ),
            "2026-09-27T12:00:00+00:00 nvidia-glm timeout "
            "model=nvidia/z-ai/glm-5.3 prompt_chars=48213 timeout=300s\n",
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
    with unittest.mock.patch(
        "model_adversarial.Popen", new_callable=PiPopen, side_effect=[empty, ok]
    ) as run:
        result = ma.run_pool([MISTRAL], "p")
    assert run.call_count == 2
    assert result == ma.PoolResult(MISTRAL, "answer")


def test_pool_exhaustion_logs_every_failure_and_the_final_event(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess1")
    (tmp_path / ".claude" / "debug").mkdir(parents=True)
    monkeypatch.setattr(ma, "datetime", _FixedDatetime)
    monkeypatch.setattr(ma.time, "monotonic", lambda: 0.0)
    empty = subprocess.CompletedProcess([], 0, "", "")
    with unittest.mock.patch(
        "model_adversarial.Popen", new_callable=PiPopen, side_effect=[empty, empty]
    ):
        ma.run_pool([MISTRAL], "p")
    log_path = tmp_path / ".claude" / "debug" / "sess1_mcp__pc-core__model-pool.txt"
    call = (
        "model=mistral/mistral-medium-3.5 prompt_chars=1 timeout=none attempt={n} "
        "tools=none elapsed=0.0s answer_chars=0"
    )
    assert log_path.read_text(encoding="utf-8") == (
        "2026-01-01T00:00:00+00:00 (pool) start entries=mistral-medium-3.5 prompt_chars=1\n"
        f"2026-01-01T00:00:00+00:00 mistral-medium-3.5 empty answer {call.format(n=1)}\n"
        f"2026-01-01T00:00:00+00:00 mistral-medium-3.5 empty answer {call.format(n=2)}\n"
        "2026-01-01T00:00:00+00:00 (pool) exhausted\n"
    )


def test_a_successful_call_is_logged_with_its_size_and_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess2")
    (tmp_path / ".claude" / "debug").mkdir(parents=True)
    monkeypatch.setattr(ma, "datetime", _FixedDatetime)
    monkeypatch.setattr(ma.time, "monotonic", iter([10.0, 12.5]).__next__)
    ok = subprocess.CompletedProcess([], 0, "answer", "")
    with unittest.mock.patch(
        "model_adversarial.Popen", new_callable=PiPopen, return_value=ok
    ):
        ma.run_pool([MISTRAL], "prompt", tools=["read", "grep"])
    log_path = tmp_path / ".claude" / "debug" / "sess2_mcp__pc-core__model-pool.txt"
    assert log_path.read_text(encoding="utf-8") == (
        "2026-01-01T00:00:00+00:00 (pool) start entries=mistral-medium-3.5 prompt_chars=6\n"
        "2026-01-01T00:00:00+00:00 mistral-medium-3.5 answered "
        "model=mistral/mistral-medium-3.5 prompt_chars=6 timeout=none attempt=1 "
        "tools=read,grep elapsed=2.5s answer_chars=6\n"
    )


def test_an_os_error_is_logged_with_its_message(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess4")
    (tmp_path / ".claude" / "debug").mkdir(parents=True)
    monkeypatch.setattr(ma, "datetime", _FixedDatetime)
    monkeypatch.setattr(ma.time, "monotonic", lambda: 0.0)
    too_big = OSError(7, "Argument list too long: 'pi'")
    with unittest.mock.patch(
        "model_adversarial.Popen", new_callable=PiPopen, side_effect=[too_big]
    ):
        ma.run_pool([MISTRAL], "p")
    log_path = tmp_path / ".claude" / "debug" / "sess4_mcp__pc-core__model-pool.txt"
    assert log_path.read_text(encoding="utf-8") == (
        "2026-01-01T00:00:00+00:00 (pool) start entries=mistral-medium-3.5 prompt_chars=1\n"
        "2026-01-01T00:00:00+00:00 mistral-medium-3.5 os error "
        "model=mistral/mistral-medium-3.5 prompt_chars=1 timeout=none attempt=1 "
        "tools=none elapsed=0.0s error=[Errno 7] Argument list too long: 'pi'\n"
        "2026-01-01T00:00:00+00:00 (pool) exhausted\n"
    )


def test_a_provider_error_is_logged_with_the_provider_own_line(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess3")
    (tmp_path / ".claude" / "debug").mkdir(parents=True)
    monkeypatch.setattr(ma, "datetime", _FixedDatetime)
    monkeypatch.setattr(ma.time, "monotonic", lambda: 0.0)
    rate_limited = subprocess.CalledProcessError(
        1, "pi", "", "warming up\n429 too many requests"
    )
    with unittest.mock.patch(
        "model_adversarial.Popen", new_callable=PiPopen, side_effect=[rate_limited]
    ):
        ma.run_pool([MISTRAL], "p")
    log_path = tmp_path / ".claude" / "debug" / "sess3_mcp__pc-core__model-pool.txt"
    assert log_path.read_text(encoding="utf-8") == (
        "2026-01-01T00:00:00+00:00 (pool) start entries=mistral-medium-3.5 prompt_chars=1\n"
        "2026-01-01T00:00:00+00:00 mistral-medium-3.5 "
        "rate limit / out of memory / insufficient credit "
        "model=mistral/mistral-medium-3.5 prompt_chars=1 timeout=none attempt=1 "
        "tools=none elapsed=0.0s error=429 too many requests\n"
        "2026-01-01T00:00:00+00:00 (pool) exhausted\n"
    )


class TestCallCli(unittest.TestCase):
    def _ok(self):
        return subprocess.CompletedProcess([], 0, "answer\n", "")

    def test_default_argv_has_no_tools_and_closes_stdin(self):
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=self._ok()
        ) as run:
            self.assertEqual(ma.call_cli(MISTRAL, "p", 30), "answer\n")
        run.assert_called_once_with(
            [
                "pi",
                "--mode",
                "json",
                "--model",
                "mistral/mistral-medium-3.5",
                "--no-tools",
                "--",
                "p",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=unittest.mock.ANY,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )

    def test_entry_thinking_is_passed_before_the_prompt(self):
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=self._ok()
        ) as run:
            ma.call_cli({**MISTRAL, "thinking": "off"}, "p", 30)
        self.assertEqual(run.call_args[0][0][-4:], ["--thinking", "off", "--", "p"])

    def test_no_entry_thinking_sends_no_thinking_flag(self):
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=self._ok()
        ) as run:
            ma.call_cli(MISTRAL, "p", 30)
        self.assertNotIn("--thinking", run.call_args[0][0])

    def test_tools_become_a_comma_separated_allowlist(self):
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=self._ok()
        ) as run:
            ma.call_cli(MISTRAL, "p", 30, ["read", "grep"])
        self.assertEqual(run.call_args[0][0][5:7], ["-t", "read,grep"])

    def test_given_env_is_layered_over_the_process_env(self):
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=self._ok()
        ) as run:
            ma.call_cli(MISTRAL, "p", 30, env={"PINKY_TEST_VAR": "layered"})
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PINKY_TEST_VAR"], "layered")
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_without_env_it_reads_secret_env(self):
        with (
            unittest.mock.patch.object(ma, "secret_env", return_value={"K": "v"}),
            unittest.mock.patch(
                "model_adversarial.Popen", new_callable=PiPopen, return_value=self._ok()
            ) as run,
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
                "model_adversarial.Popen",
                new_callable=PiPopen,
                side_effect=[rate_limited, ok],
            ) as run,
        ):
            ma.run_pool([MISTRAL, GLM], "p")
        self.assertEqual(secrets.call_count, 1)
        self.assertEqual([c.kwargs["env"]["K"] for c in run.call_args_list], ["v", "v"])

    def test_check_pool_reads_the_keychain_once(self):
        ok = subprocess.CompletedProcess([], 0, "OK", "")
        with (
            unittest.mock.patch.object(ma, "secret_env", return_value={}) as secrets,
            unittest.mock.patch(
                "model_adversarial.Popen", new_callable=PiPopen, return_value=ok
            ),
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
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=ok
        ) as run:
            ma.run_pool([slow], "p", timeout=180)
        self.assertEqual(run.wait_timeouts, [300])

    def test_missing_entry_timeout_keeps_an_absent_default(self):
        self.assertIsNone(ma.entry_timeout(MISTRAL, None))

    def test_entry_timeout_replaces_an_absent_default(self):
        self.assertEqual(ma.entry_timeout({**MISTRAL, "timeout": 300}, None), 300)

    def test_run_pool_gives_an_entry_with_a_timeout_that_bound_over_no_default(self):
        ok = subprocess.CompletedProcess([], 0, "answer", "")
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=ok
        ) as run:
            ma.run_pool([{**GLM, "timeout": 300}, MISTRAL], "p", timeout=None)
        self.assertEqual(run.wait_timeouts, [300])

    def test_check_pool_keeps_its_own_60_seconds(self):
        ok = subprocess.CompletedProcess([], 0, "OK", "")
        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, return_value=ok
        ) as run:
            ma.check_pool([MISTRAL])
        self.assertEqual(run.wait_timeouts, [60])

    def test_check_pool_waits_the_entry_timeout(self):
        with unittest.mock.patch(
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=subprocess.TimeoutExpired("pi", 300),
        ) as run:
            (result,) = ma.check_pool([{**GLM, "timeout": 300}], timeout=60)
        self.assertEqual(run.wait_timeouts[0], 300)
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
            if cmd[4] == "openrouter/z-ai/glm-5-2":
                raise subprocess.CalledProcessError(1, cmd, "", "401 unauthorized\n")
            return subprocess.CompletedProcess(cmd, 0, "OK\n", "")

        with unittest.mock.patch(
            "model_adversarial.Popen", new_callable=PiPopen, side_effect=fake_run
        ):
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
            "model_adversarial.Popen",
            new_callable=PiPopen,
            side_effect=subprocess.TimeoutExpired("pi", 5),
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
    import pc_core_server

    src = tmp_path / "api.py"
    src.write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n", encoding="utf-8"
    )

    captured: dict[str, str] = {}

    def fake_run_pool(entries, prompt, timeout=180, tools=None, watch=False):
        captured["prompt"] = prompt
        return ma.PoolResult(entries[0], "ok")

    monkeypatch.setattr(pc_core_server.model_adversarial, "run_pool", fake_run_pool)

    out = pc_core_server.adversarial(task="review this", files=[str(src)])
    # a phrase unique to the FastAPI review reference, not the generic methodology
    assert "N+1 query pattern" in captured["prompt"]
    assert out.startswith("[answered by ")


def test_adversarial_tool_ignores_an_unknown_tier(monkeypatch):
    """An unrecognised tier must not route — fall back to the default adversarial selection."""
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp")
    )
    import pc_core_server

    seen: dict[str, list] = {}

    def fake_select_for_adversarial(pool, floor_tier="sonnet", rng=None):
        seen["adversarial"] = pool
        return [MISTRAL]

    def fake_select_for_delegation(pool, task_tier="haiku"):
        seen["delegation"] = task_tier
        return [MISTRAL]

    monkeypatch.setattr(
        pc_core_server.model_adversarial,
        "select_for_adversarial",
        fake_select_for_adversarial,
    )
    monkeypatch.setattr(
        pc_core_server.model_adversarial,
        "select_for_delegation",
        fake_select_for_delegation,
    )
    monkeypatch.setattr(
        pc_core_server.model_adversarial,
        "run_pool",
        lambda entries, prompt, timeout=180, tools=None, watch=False: ma.PoolResult(
            entries[0], "ok"
        ),
    )

    pc_core_server.adversarial(task="review", tier="turbo-9000")
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
        lambda entries, prompt, timeout=180, tools=None, watch=False: ma.PoolResult(
            None, "everything failed"
        ),
    )

    # `model="x"` bypasses `load_pool` via the override path, straight to `run_pool`.
    out = pc_core_server.adversarial(task="review", model="x")
    assert (
        out == "[adversarial] BLOCKED — every candidate failed. Last: everything failed"
    )


def _server():
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp")
    )
    import pc_core_server

    return pc_core_server


def test_adversarial_tool_with_base_reviews_the_computed_diff_and_routes_its_tier(
    monkeypatch,
):
    server = _server()
    seen: dict = {}
    small_diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )

    monkeypatch.setattr(
        server.model_adversarial, "diff_against", lambda base, scope="all": small_diff
    )
    monkeypatch.setattr(server.model_adversarial, "repo_root", lambda: "/work/repo")
    monkeypatch.setattr(server.review_complexity, "tier_for", lambda d: "opus")

    def fake_select(pool, tier, floor_tier="haiku", rng=None):
        seen["tier"] = tier
        return [MISTRAL]

    monkeypatch.setattr(
        server.model_adversarial, "select_for_delegation_with_fallback", fake_select
    )

    def fake_run_pool(entries, prompt, timeout=180, tools=None, watch=False):
        seen["prompt"] = prompt
        return ma.PoolResult(entries[0], "ok")

    monkeypatch.setattr(server.model_adversarial, "run_pool", fake_run_pool)

    out = server.adversarial(task="review this", base="main")

    assert out.startswith("[answered by ")
    assert seen["tier"] == "opus"
    assert "--- DIFF against main in /work/repo ---\n" + small_diff in seen["prompt"]


@pytest.mark.parametrize("tier_arg", [None, "haiku"])
def test_adversarial_tool_never_routes_a_small_diff_below_the_sonnet_floor(
    monkeypatch, tier_arg
):
    server = _server()
    small_diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    free_haiku = _entry("free-haiku", "openrouter", "haiku")
    free_sonnet = _entry("free-sonnet", "nvidia", "sonnet")
    seen: dict = {}

    monkeypatch.setattr(
        server.model_adversarial, "diff_against", lambda base, scope="all": small_diff
    )
    monkeypatch.setattr(server.model_adversarial, "repo_root", lambda: "/work/repo")
    monkeypatch.setattr(
        server.model_adversarial, "load_pool", lambda: [free_haiku, free_sonnet]
    )

    def fake_run_pool(entries, prompt, timeout=180, tools=None, watch=False):
        seen["entries"] = entries
        return ma.PoolResult(entries[0], "ok")

    monkeypatch.setattr(server.model_adversarial, "run_pool", fake_run_pool)

    server.adversarial(task="review this", base="main", tier=tier_arg)

    assert seen["entries"] == [free_sonnet]


def test_adversarial_tool_blocks_on_a_haiku_only_pool_without_calling_it(monkeypatch):
    server = _server()
    small_diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    monkeypatch.setattr(
        server.model_adversarial, "diff_against", lambda base, scope="all": small_diff
    )
    monkeypatch.setattr(server.model_adversarial, "repo_root", lambda: "/work/repo")
    monkeypatch.setattr(
        server.model_adversarial,
        "load_pool",
        lambda: [_entry("free-haiku", "openrouter", "haiku")],
    )

    def boom(*args, **kwargs):
        raise AssertionError("the pool must not be called")

    monkeypatch.setattr(server.model_adversarial, "run_pool", boom)

    assert server.adversarial(task="review", base="main") == (
        "[adversarial] BLOCKED — no candidate model in the pool."
    )


def test_fallback_floor_raises_a_low_tier_and_stops_degrading():
    haiku = _entry("free-haiku", "openrouter", "haiku")
    sonnet = _entry("free-sonnet", "nvidia", "sonnet")
    assert ma.select_for_delegation_with_fallback(
        [haiku, sonnet], "haiku", floor_tier="sonnet"
    ) == [sonnet]
    assert (
        ma.select_for_delegation_with_fallback([haiku], "opus", floor_tier="sonnet")
        == []
    )


def test_adversarial_tool_with_base_and_no_change_does_not_call_the_pool(monkeypatch):
    server = _server()
    monkeypatch.setattr(
        server.model_adversarial, "diff_against", lambda base, scope="all": ""
    )
    monkeypatch.setattr(server.model_adversarial, "repo_root", lambda: "/work/repo")

    def boom(*args, **kwargs):
        raise AssertionError("the pool must not be called")

    monkeypatch.setattr(server.model_adversarial, "run_pool", boom)

    assert (
        server.adversarial(task="review", base="main")
        == "[adversarial] nothing to review — no change against 'main' in /work/repo."
    )


def test_adversarial_tool_with_an_unusable_base_reports_the_git_error(monkeypatch):
    server = _server()

    def fail(base, scope="all"):
        raise subprocess.CalledProcessError(
            128, "git", "", "fatal: Not a valid object name nope"
        )

    monkeypatch.setattr(server.model_adversarial, "diff_against", fail)
    monkeypatch.setattr(server.model_adversarial, "repo_root", lambda: "/work/repo")

    assert server.adversarial(task="review", base="nope") == (
        "[adversarial] ERROR — cannot diff against 'nope' in /work/repo: "
        "fatal: Not a valid object name nope"
    )


def test_adversarial_tool_header_names_the_provider_model_and_tier(monkeypatch):
    server = _server()
    entry = _entry("nvidia-glm-5.3", "nvidia", "opus", model="z-ai/glm-5.3")
    monkeypatch.setattr(
        server.model_adversarial,
        "run_pool",
        lambda entries, prompt, timeout=180, tools=None, watch=False: ma.PoolResult(
            entry, "verdict"
        ),
    )

    out = server.adversarial(task="review", model="x")

    assert out == (
        "[answered by nvidia/glm-5.3 · 🟧 opus]\n\nverdict"
        f"\n\n[raw: {server.model_sanitize.raw_log_path()}]"
    )


def test_adversarial_tool_forwards_the_diff_scope(monkeypatch):
    server = _server()
    seen: dict = {}

    def fake_diff(base, scope="all"):
        seen["scope"] = scope
        return ""

    monkeypatch.setattr(server.model_adversarial, "diff_against", fake_diff)
    monkeypatch.setattr(server.model_adversarial, "repo_root", lambda: "/work/repo")

    server.adversarial(task="review", base="main", scope="staged")

    assert seen["scope"] == "staged"


def test_select_for_adversarial_without_rng_keeps_pool_order():
    """The selection itself is deterministic; the draw comes from the caller's rng."""
    ol_a = _entry("ollama-a", "ollama-cloud", priority=1)
    ol_b = _entry("ollama-b", "ollama-cloud", priority=1)
    assert ma.select_for_adversarial([ol_b, ol_a]) == [ol_b, ol_a]


def test_adversarial_cli_draws_with_a_fresh_rng(monkeypatch):
    """`main` hands `select_for_adversarial` a `random.Random`, so a served review varies."""
    seen: dict[str, object] = {}

    def spy(pool, floor_tier="sonnet", rng=None):
        seen["rng"] = rng
        return [MISTRAL]

    monkeypatch.setattr(ma, "select_for_adversarial", spy)
    monkeypatch.setattr(ma, "load_pool", lambda: [MISTRAL])
    monkeypatch.setattr(
        ma,
        "run_pool",
        lambda entries, prompt, timeout=180, tools=None, watch=False: ma.PoolResult(
            entries[0], "ok"
        ),
    )
    assert ma.main(["--task", "review this"]) == 0
    assert isinstance(seen["rng"], random.Random)


def test_adversarial_tool_draws_with_a_fresh_rng(monkeypatch):
    """The MCP `adversarial` tool hands `select_for_adversarial` a `random.Random`."""
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp")
    )
    import pc_core_server

    seen: dict[str, object] = {}

    def spy(pool, floor_tier="sonnet", rng=None):
        seen["rng"] = rng
        return [MISTRAL]

    monkeypatch.setattr(pc_core_server.model_adversarial, "select_for_adversarial", spy)
    monkeypatch.setattr(
        pc_core_server.model_adversarial,
        "run_pool",
        lambda entries, prompt, timeout=180, tools=None, watch=False: ma.PoolResult(
            entries[0], "ok"
        ),
    )
    out = pc_core_server.adversarial(task="review this")
    assert out.startswith("[answered by ")
    assert isinstance(seen["rng"], random.Random)


def _heads_served_for_a_diff(monkeypatch, tmp_path, pool, calls=20):
    """The entry opening the candidate list at each of `calls` calls of the `adversarial` tool
    carrying a diff — the real selector runs, only the pool read and the model call are faked."""
    server = _server()
    diff = tmp_path / "change.diff"
    diff.write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
        encoding="utf-8",
    )
    draws = iter(random.Random(seed) for seed in range(calls))
    heads: list[str] = []

    monkeypatch.setattr(
        server, "random", types.SimpleNamespace(Random=lambda: next(draws))
    )
    monkeypatch.setattr(server.model_adversarial, "load_pool", lambda: pool)

    def fake_run_pool(entries, prompt, timeout=180, tools=None, watch=False):
        heads.append(entries[0]["name"])
        return ma.PoolResult(entries[0], "ok")

    monkeypatch.setattr(server.model_adversarial, "run_pool", fake_run_pool)

    for _ in range(calls):
        server.adversarial(task="review", diff=str(diff))
    return heads


def test_adversarial_tool_with_a_diff_draws_among_entries_of_equal_priority(
    monkeypatch, tmp_path
):
    pool = [
        _entry("ol-a", "ollama-cloud", priority=1),
        _entry("ol-b", "ollama-cloud", priority=1),
        _entry("ol-c", "ollama-cloud", priority=1),
    ]
    heads = _heads_served_for_a_diff(monkeypatch, tmp_path, pool)
    assert set(heads) == {"ol-a", "ol-b", "ol-c"}


def test_adversarial_tool_with_a_diff_opens_on_the_higher_priority_entry(
    monkeypatch, tmp_path
):
    pool = [
        _entry("ol-a", "ollama-cloud", priority=1),
        _entry("ol-b", "ollama-cloud", priority=1),
        _entry("ol-top", "ollama-cloud", priority=2),
    ]
    heads = _heads_served_for_a_diff(monkeypatch, tmp_path, pool)
    assert set(heads) == {"ol-top"}


def _entries_served_for_model(monkeypatch, pool, model):
    """The candidate list the `adversarial` tool hands the pool when called with `model` — only
    the pool read, the model call and the rng are faked."""
    server = _server()
    seen: dict[str, list] = {}

    monkeypatch.setattr(
        server, "random", types.SimpleNamespace(Random=lambda: random.Random(0))
    )
    monkeypatch.setattr(server.model_adversarial, "load_pool", lambda: pool)

    def fake_run_pool(entries, prompt, timeout=180, tools=None, watch=False):
        seen["entries"] = entries
        return ma.PoolResult(entries[0], "ok")

    monkeypatch.setattr(server.model_adversarial, "run_pool", fake_run_pool)

    server.adversarial(task="review", model=model)
    return seen["entries"]


def test_adversarial_tool_model_naming_a_pool_entry_calls_that_entry_as_is(
    monkeypatch,
):
    nemotron = _entry(
        "ollama-nemotron-3-ultra",
        "ollama-cloud",
        "haiku",
        model="nemotron-3-ultra",
        timeout=900,
        thinking="high",
    )
    pool = [_entry("nvidia-glm", "nvidia", "opus"), nemotron]

    entries = _entries_served_for_model(monkeypatch, pool, "ollama-nemotron-3-ultra")

    assert entries == [nemotron]


def test_adversarial_tool_model_naming_no_pool_entry_is_a_model_id_at_the_default_provider(
    monkeypatch,
):
    pool = [_entry("nvidia-glm", "nvidia", "opus")]

    entries = _entries_served_for_model(monkeypatch, pool, "z-ai/glm-5.3-flash")

    assert [(e["provider"], e["model"]) for e in entries] == [
        ("nvidia", "z-ai/glm-5.3-flash")
    ]


def _fake_binary(directory, name, body):
    """An executable `name` in `directory`: a POSIX shell script running `body`."""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _marker(entry, attempt):
    """The line `run_pool` writes into the events file before an attempt."""
    return (
        json.dumps(
            {"type": "pool_attempt", "entry": entry, "attempt": attempt},
            separators=(",", ":"),
        )
        + "\n"
    )


def _result(entry, attempt, outcome):
    """The line `run_pool` writes into the events file after an attempt."""
    record = {
        "type": "pool_result",
        "entry": entry,
        "attempt": attempt,
        "outcome": outcome,
    }
    return json.dumps(record, separators=(",", ":")) + "\n"


_POOL_END = '{"type":"pool_end"}\n'

_PI_SCRIPT = """\
d="{dir}"
n=$(($(/bin/cat "$d/count" 2>/dev/null || echo 0) + 1))
echo $n > "$d/count"
printf '%s\\n' "$@" > "$d/argv.$n"
/bin/cat "$d/out.$n" 2>/dev/null
/bin/cat "$d/err.$n" >&2 2>/dev/null
[ -e "$d/hang.$n" ] && exec /bin/sleep 30
exit $(/bin/cat "$d/rc.$n" 2>/dev/null || echo 0)"""


class TestPoolRun:
    """`run_pool` against fake `pi` and `cmux` executables first on `PATH`: `call_cli`, its
    reading of the event stream and the pane's launch all run for real."""

    @pytest.fixture
    def bins(self, tmp_path, monkeypatch):
        """A `PATH` of one directory holding a `pi` that records its arguments and prints the
        `out.<n>` / `err.<n>` files of its n-th call, then exits with `rc.<n>`."""
        directory = tmp_path / "bin"
        directory.mkdir()
        _fake_binary(directory, "pi", _PI_SCRIPT.format(dir=tmp_path))
        monkeypatch.setenv("PATH", str(directory))
        return directory

    def _pi_prints(self, tmp_path, *outputs, errors=(), codes=()):
        """What the n-th `pi` call prints on stdout, on stderr, and its exit status."""
        for n, text in enumerate(outputs, start=1):
            (tmp_path / f"out.{n}").write_text(text)
        for n, text in enumerate(errors, start=1):
            (tmp_path / f"err.{n}").write_text(text)
        for n, code in enumerate(codes, start=1):
            (tmp_path / f"rc.{n}").write_text(str(code))

    def _pi_calls(self, tmp_path):
        path = tmp_path / "count"
        return int(path.read_text()) if path.exists() else 0

    def _cmux_argv(self, tmp_path):
        """The arguments the fake `cmux` recorded, one per line; None if it never ran."""
        path = tmp_path / "cmux.argv"
        return path.read_text().splitlines() if path.exists() else None

    def _recording_cmux(self, bins, tmp_path):
        # `>>` : a second launch would add lines, so equality proves a single one.
        _fake_binary(bins, "cmux", f'printf "%s\\n" "$@" >> "{tmp_path}/cmux.argv"')

    # --- the split (one per run, to the right, following every attempt) -------------------

    def test_a_run_with_a_requeue_opens_one_split_that_follows_every_attempt(
        self, bins, tmp_path, monkeypatch
    ):
        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        self._pi_prints(tmp_path, pi_events(""), pi_events(""), pi_events("answer"))

        result = ma.run_pool([MISTRAL, GLM], "p", watch=True)

        argv = self._cmux_argv(tmp_path)
        events_path = Path(shlex.split(argv[3])[2])
        viewer = Path(ma.__file__).with_name("pi_events_view.py")
        assert result == ma.PoolResult(MISTRAL, "answer")
        assert argv == [
            "new-split",
            "right",
            "--command",
            shlex.join([sys.executable, str(viewer), str(events_path)]),
            "--focus",
            "false",
        ]
        assert self._pi_calls(tmp_path) == 3
        assert events_path.read_text() == (
            _marker("mistral-medium-3.5", 1)
            + pi_events("")
            + _result("mistral-medium-3.5", 1, "empty answer")
            + _marker("glm-5-2", 1)
            + pi_events("")
            + _result("glm-5-2", 1, "empty answer")
            + _marker("mistral-medium-3.5", 2)
            + pi_events("answer")
            + _result("mistral-medium-3.5", 2, "answered")
            + _POOL_END
        )
        assert stat.S_IMODE(events_path.parent.stat().st_mode) == 0o700

    def test_each_run_gets_its_own_private_events_file(
        self, bins, tmp_path, monkeypatch
    ):
        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        self._pi_prints(tmp_path, pi_events("answer"), pi_events("answer"))

        ma.run_pool([MISTRAL], "p", watch=True)
        ma.run_pool([MISTRAL], "p", watch=True)

        launches = self._cmux_argv(tmp_path)
        assert len(launches) == 12
        assert launches[3] != launches[9]

    def test_outside_cmux_no_split_and_the_same_result(self, bins, tmp_path):
        self._recording_cmux(bins, tmp_path)
        self._pi_prints(tmp_path, pi_events("answer"))

        result = ma.run_pool([MISTRAL], "p", watch=True)

        assert result == ma.PoolResult(MISTRAL, "answer")
        assert self._cmux_argv(tmp_path) is None

    # --- a failing split never changes the result -----------------------------------------

    @pytest.mark.parametrize(
        "cmux_body",
        ["exit 1", None, "exec /bin/sleep 30"],
        ids=["fails", "absent", "hangs"],
    )
    def test_a_split_that_cannot_open_leaves_the_result_unchanged_and_bounded(
        self, bins, tmp_path, monkeypatch, cmux_body
    ):
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        monkeypatch.setattr(ma, "_PANE_TIMEOUT", 0.3)
        if cmux_body is not None:
            _fake_binary(bins, "cmux", cmux_body)
        self._pi_prints(tmp_path, pi_events("answer"))

        started = time.monotonic()
        result = ma.run_pool([MISTRAL], "p", watch=True)

        assert result == ma.PoolResult(MISTRAL, "answer")
        assert time.monotonic() - started < 5

    def test_cmux_output_never_reaches_this_process_stdout(
        self, bins, tmp_path, monkeypatch, capfd
    ):
        # The MCP server's stdout is its JSON-RPC channel.
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        _fake_binary(bins, "cmux", "echo noise; echo noise >&2")
        self._pi_prints(tmp_path, pi_events("answer"))

        ma.run_pool([MISTRAL], "p", watch=True)

        assert capfd.readouterr() == ("", "")

    # --- only run_pool, and only when asked, opens a split --------------------------------

    def test_a_run_that_does_not_ask_never_opens_a_split(
        self, bins, tmp_path, monkeypatch
    ):
        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        self._pi_prints(tmp_path, pi_events("answer"))

        assert ma.run_pool([MISTRAL], "p") == ma.PoolResult(MISTRAL, "answer")
        assert self._cmux_argv(tmp_path) is None

    def test_check_pool_panel_and_cli_never_open_a_split(
        self, bins, tmp_path, monkeypatch
    ):
        import model_panel

        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        self._pi_prints(tmp_path, *[pi_events("OK")] * 8)
        pool = [_entry(f"entry-{i}", "nvidia") for i in range(3)]

        checks = ma.check_pool(pool)
        panel = model_panel.run_panel(pool[:2], ["t1", "t2"])
        monkeypatch.setattr(ma, "load_pool", lambda: [MISTRAL])
        status = ma.main(["--task", "t"])

        assert [c.status for c in checks] == ["ok", "ok", "ok"]
        assert panel is not None
        assert status == 0
        assert self._cmux_argv(tmp_path) is None

    # --- the outcome is read from the events ----------------------------------------------

    def test_call_cli_runs_pi_in_json_mode_and_returns_the_last_message_text(
        self, bins, tmp_path
    ):
        events = (
            json.dumps({"type": "agent_start"})
            + "\n"
            + json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "content": [{"type": "text", "text": "first turn"}],
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [
                            {"type": "thinking", "thinking": "hm"},
                            {"type": "text", "text": "verdict: "},
                            {"type": "text", "text": "no finding"},
                        ],
                    },
                }
            )
            + "\n"
        )
        self._pi_prints(tmp_path, events)
        copy = io.StringIO()

        answer = ma.call_cli(MISTRAL, "p", 10, ["read"], env={}, events=copy)

        assert answer == "verdict: no finding"
        assert (tmp_path / "argv.1").read_text().splitlines() == [
            "--mode",
            "json",
            "--model",
            "mistral/mistral-medium-3.5",
            "-t",
            "read",
            "--",
            "p",
        ]
        assert copy.getvalue() == events

    def test_a_final_429_falls_over_to_the_next_entry(self, bins, tmp_path):
        self._pi_prints(
            tmp_path,
            pi_events("", "error", '429 {"message":"rate limited"}\n'),
            pi_events("second"),
        )

        result = ma.run_pool([MISTRAL, GLM], "p")

        assert result == ma.PoolResult(GLM, "second")
        assert self._pi_calls(tmp_path) == 2

    def test_a_429_that_pi_retried_past_is_a_success(self, bins, tmp_path):
        failed = json.loads(pi_events("", "error", "429 slow down").splitlines()[2])
        answered = json.loads(pi_events("final").splitlines()[2])
        stream = "".join(
            json.dumps(e) + "\n"
            for e in (
                failed,
                {"type": "auto_retry_start", "attempt": 1, "errorMessage": "429"},
                {"type": "auto_retry_end", "success": True, "attempt": 2},
                answered,
                {"type": "agent_settled", "aborted": False},
            )
        )
        self._pi_prints(tmp_path, stream)

        assert ma.run_pool([MISTRAL, GLM], "p") == ma.PoolResult(MISTRAL, "final")
        assert self._pi_calls(tmp_path) == 1

    def test_an_unrecognised_error_stops_the_pool_with_the_providers_line_last(
        self, bins, tmp_path
    ):
        self._pi_prints(
            tmp_path,
            pi_events("", "error", "401 invalid api key\n"),
            pi_events("never asked"),
            errors=["warning: models.json is slow\n"],
        )

        result = ma.run_pool([MISTRAL, GLM], "p")

        assert result == ma.PoolResult(
            None,
            "mistral-medium-3.5 critical failure: warning: models.json is slow\n"
            "401 invalid api key",
        )
        assert self._pi_calls(tmp_path) == 1

    def test_a_crash_with_a_plain_stdout_line_carries_that_line(self, bins, tmp_path):
        self._pi_prints(tmp_path, "boom, not json\n", codes=[2])

        result = ma.run_pool([MISTRAL], "p")

        assert result == ma.PoolResult(
            None, "mistral-medium-3.5 critical failure: boom, not json"
        )

    def test_a_429_in_the_streamed_text_is_not_a_rate_limit(self, bins, tmp_path):
        # The event stream is not error text: a review that mentions "429" or "timed out" must
        # not turn another failure into a fall-over.
        stream = pi_events("the code handles 429 and timed out").replace(
            '"stopReason": "stop"', '"stopReason": "error"'
        )
        self._pi_prints(tmp_path, stream)

        result = ma.run_pool([MISTRAL, GLM], "p")

        assert result == ma.PoolResult(
            None, "mistral-medium-3.5 critical failure: pi ended on an error"
        )
        assert self._pi_calls(tmp_path) == 1

    def test_an_aborted_run_is_a_failure_with_a_fallback_detail(self, bins, tmp_path):
        # pi's JSON mode exits 0 on an abort; its text mode exits 1 with "Request aborted".
        self._pi_prints(tmp_path, pi_events("", "aborted"))

        result = ma.run_pool([MISTRAL, GLM], "p")

        assert result == ma.PoolResult(
            None, "mistral-medium-3.5 critical failure: Request aborted"
        )
        assert self._pi_calls(tmp_path) == 1

    def test_an_aborted_run_reports_its_own_error_message(self, bins, tmp_path):
        self._pi_prints(
            tmp_path, pi_events("partial", "aborted", "Operation aborted by user")
        )

        result = ma.run_pool([MISTRAL], "p")

        assert result == ma.PoolResult(
            None, "mistral-medium-3.5 critical failure: Operation aborted by user"
        )

    def test_a_line_cut_by_the_kill_does_not_swallow_the_next_marker(
        self, bins, tmp_path, monkeypatch
    ):
        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        cut = '{"type":"message_upd'
        self._pi_prints(tmp_path, cut, pi_events("answer"))
        (tmp_path / "hang.1").write_text("")

        result = ma.run_pool([MISTRAL, GLM], "p", timeout=1, watch=True)

        events_path = Path(shlex.split(self._cmux_argv(tmp_path)[3])[2])
        assert result == ma.PoolResult(GLM, "answer")
        assert events_path.read_text() == (
            _marker("mistral-medium-3.5", 1)
            + cut
            + "\n"
            + _result("mistral-medium-3.5", 1, "timeout")
            + _marker("glm-5-2", 1)
            + pi_events("answer")
            + _result("glm-5-2", 1, "answered")
            + _POOL_END
        )

    def test_pool_end_is_written_even_when_an_attempt_raises(
        self, bins, tmp_path, monkeypatch
    ):
        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        monkeypatch.setattr(
            ma, "call_cli", unittest.mock.Mock(side_effect=RuntimeError("boom"))
        )

        with pytest.raises(RuntimeError, match="boom"):
            ma.run_pool([MISTRAL], "p", watch=True)

        events_path = Path(shlex.split(self._cmux_argv(tmp_path)[3])[2])
        assert events_path.read_text() == _marker("mistral-medium-3.5", 1) + _POOL_END
        assert stat.S_IMODE(events_path.parent.stat().st_mode) == 0o700

    def test_the_pane_is_told_how_each_attempt_ended(self, bins, tmp_path, monkeypatch):
        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        text_call = "<function=read>\n<parameter=path>a.py</parameter>\n</function>"
        self._pi_prints(
            tmp_path,
            pi_events("", "error", "429 slow down"),
            pi_events(text_call),
            pi_events(""),
            pi_events("answer"),
        )

        result = ma.run_pool([MISTRAL, GLM, KIMI], "p", watch=True)

        events_path = Path(shlex.split(self._cmux_argv(tmp_path)[3])[2])
        assert result == ma.PoolResult(GLM, "answer")
        assert events_path.read_text() == (
            _marker("mistral-medium-3.5", 1)
            + pi_events("", "error", "429 slow down")
            + _result("mistral-medium-3.5", 1, "rate limited")
            + _marker("glm-5-2", 1)
            + pi_events(text_call)
            + _result("glm-5-2", 1, "tool call text")
            + _marker("kimi-k2.6", 1)
            + pi_events("")
            + _result("kimi-k2.6", 1, "empty answer")
            + _marker("glm-5-2", 2)
            + pi_events("answer")
            + _result("glm-5-2", 2, "answered")
            + _POOL_END
        )

    def test_the_pane_is_told_the_provider_line_of_a_failure(
        self, bins, tmp_path, monkeypatch
    ):
        self._recording_cmux(bins, tmp_path)
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        self._pi_prints(tmp_path, pi_events("", "error", "401 invalid api key"))

        ma.run_pool([MISTRAL], "p", watch=True)

        events_path = Path(shlex.split(self._cmux_argv(tmp_path)[3])[2])
        assert events_path.read_text() == (
            _marker("mistral-medium-3.5", 1)
            + pi_events("", "error", "401 invalid api key")
            + _result("mistral-medium-3.5", 1, "failed: 401 invalid api key")
            + _POOL_END
        )

    def test_exit_0_without_any_message_but_with_output_is_a_failure(
        self, bins, tmp_path
    ):
        self._pi_prints(tmp_path, "usage: pi [options]\n")

        result = ma.run_pool([MISTRAL, GLM], "p")

        assert result == ma.PoolResult(
            None, "mistral-medium-3.5 critical failure: usage: pi [options]"
        )

    def test_exit_0_without_any_message_but_with_stderr_is_a_failure(
        self, bins, tmp_path
    ):
        self._pi_prints(tmp_path, "", errors=["no models configured\n"])

        result = ma.run_pool([MISTRAL, GLM], "p")

        assert result == ma.PoolResult(
            None, "mistral-medium-3.5 critical failure: no models configured"
        )

    def test_exit_0_with_nothing_at_all_is_an_empty_answer_requeued_once(
        self, bins, tmp_path
    ):
        self._pi_prints(tmp_path, "", "")

        result = ma.run_pool([MISTRAL], "p")

        assert result == ma.PoolResult(None, "mistral-medium-3.5: empty answer")
        assert self._pi_calls(tmp_path) == 2

    def test_a_child_that_pi_left_holding_the_pipes_is_killed_with_it(
        self, bins, tmp_path
    ):
        # A tool-spawned process inherits pi's pipes: killing pi alone would leave the pool
        # waiting on them.
        _fake_binary(
            bins,
            "pi",
            f'/bin/sleep 30 &\necho $! > "{tmp_path}/child.pid"\nexec /bin/sleep 30',
        )

        started = time.monotonic()
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                ma.call_cli(MISTRAL, "p", 1, env={})
            elapsed = time.monotonic() - started
            child = int((tmp_path / "child.pid").read_text())
            for _ in range(20):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                os.kill(child, signal.SIGKILL)
                pytest.fail("the child of pi is still running")
        finally:
            pid_file = tmp_path / "child.pid"
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

        assert elapsed < 4

    def test_a_large_stream_on_both_pipes_at_once_completes(self, bins, tmp_path):
        script = tmp_path / "big.py"
        script.write_text(
            "import json, sys, threading\n"
            "def err():\n"
            "    for i in range(2000):\n"
            "        sys.stderr.write('warning %d ' % i + 'x' * 90 + '\\n')\n"
            "        sys.stderr.flush()\n"
            "t = threading.Thread(target=err)\n"
            "t.start()\n"
            "delta = {'type': 'message_update', 'assistantMessageEvent':\n"
            "         {'type': 'text_delta', 'contentIndex': 0, 'delta': 'y' * 90}}\n"
            "for i in range(2000):\n"
            "    sys.stdout.write(json.dumps(delta) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "end = {'type': 'message_end', 'message': {'role': 'assistant',\n"
            "       'stopReason': 'stop', 'content': [{'type': 'text', 'text': 'big answer'}]}}\n"
            "sys.stdout.write(json.dumps(end) + '\\n')\n"
            "t.join()\n"
        )
        _fake_binary(bins, "pi", f'exec "{sys.executable}" "{script}"')
        copy = io.StringIO()

        started = time.monotonic()
        answer = ma.call_cli(MISTRAL, "p", 60, env={}, events=copy)

        assert answer == "big answer"
        assert len(copy.getvalue().splitlines()) == 2001
        assert time.monotonic() - started < 30

    def _recording_popen(self, interrupt=False):
        """A `Popen` that records `kill()` and, when `interrupt`, raises `KeyboardInterrupt`
        from the timed `wait` — the process behind it is real."""
        kills = []

        class RecordingPopen(subprocess.Popen):
            def kill(self):
                kills.append(self.pid)
                super().kill()

            def wait(self, timeout=None):
                if interrupt and timeout is not None:
                    raise KeyboardInterrupt
                return super().wait(timeout)

        return RecordingPopen, kills

    def test_a_timeout_still_kills_where_there_is_no_process_groups(
        self, bins, monkeypatch
    ):
        # Windows has no `os.killpg`: the plain `kill()` must still be reached.
        _fake_binary(bins, "pi", "exec /bin/sleep 30")
        monkeypatch.delattr(os, "killpg")
        popen, kills = self._recording_popen()
        monkeypatch.setattr(ma, "Popen", popen)

        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            ma.call_cli(MISTRAL, "p", 1, env={})

        assert len(kills) == 1
        assert time.monotonic() - started < 10

    def test_an_interrupt_while_waiting_kills_the_detached_session(
        self, bins, monkeypatch
    ):
        # `start_new_session` detached pi from the terminal's group: Ctrl-C never reaches it.
        _fake_binary(bins, "pi", "exec /bin/sleep 30")
        popen, kills = self._recording_popen(interrupt=True)
        monkeypatch.setattr(ma, "Popen", popen)

        with pytest.raises(KeyboardInterrupt):
            ma.call_cli(MISTRAL, "p", 60, env={})

        assert len(kills) == 1
        for _ in range(20):
            try:
                os.kill(kills[0], 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(kills[0], signal.SIGKILL)
            pytest.fail("pi is still running after the interrupt")

    def test_an_entry_without_timeout_waits_for_a_slow_run(self, bins, tmp_path):
        (tmp_path / "out.1").write_text(pi_events("slow answer"))
        _fake_binary(bins, "pi", f'/bin/sleep 2\n/bin/cat "{tmp_path}/out.1"')

        result = ma.run_pool([MISTRAL], "p")

        assert result == ma.PoolResult(MISTRAL, "slow answer")

    def test_an_entry_with_a_timeout_is_cut_and_its_session_killed(
        self, bins, tmp_path, monkeypatch
    ):
        _fake_binary(bins, "pi", "exec /bin/sleep 30")
        popen, kills = self._recording_popen()
        monkeypatch.setattr(ma, "Popen", popen)

        started = time.monotonic()
        result = ma.run_pool([{**MISTRAL, "timeout": 1}], "p")

        assert result.entry is None
        assert "timed out after 1" in str(result.result)
        assert len(kills) == 1
        assert time.monotonic() - started < 10

    def test_a_run_past_its_timeout_is_killed(self, bins, tmp_path):
        _fake_binary(bins, "pi", "exec /bin/sleep 30")

        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            ma.call_cli(MISTRAL, "p", 1, env={})

        assert time.monotonic() - started < 10


class TestFakePiProcess:
    def test_a_canned_timeout_is_raised_once_and_the_process_is_reaped_after_kill(self):
        from pi_fakes import FakePiProcess

        waits = []
        proc = FakePiProcess("", "", 0, subprocess.TimeoutExpired("pi", 5), waits)

        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        proc.kill()

        assert proc.wait() == 0
        assert waits == [5, None]


class TestAdversarialToolWatches:
    def test_the_adversarial_tool_asks_for_the_split(self, monkeypatch):
        server = _server()
        seen = {}

        def fake_run_pool(entries, prompt, timeout=180, tools=None, watch=False):
            seen["watch"] = watch
            return ma.PoolResult(entries[0], "ok")

        monkeypatch.setattr(server.model_adversarial, "load_pool", lambda: [MISTRAL])
        monkeypatch.setattr(server.model_adversarial, "run_pool", fake_run_pool)

        server.adversarial(task="review")

        assert seen == {"watch": True}


# --- the viewer ------------------------------------------------------------------------

_EVENTS = [
    {"type": "session", "id": "66e1b811-6b2d-460e-b3c3-eee7d057a75a"},
    {"type": "pool_attempt", "entry": "nvidia-glm", "attempt": 1},
    {"type": "agent_start"},
    {
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
    },
    {
        "type": "tool_execution_start",
        "toolName": "read",
        "args": {
            "path": "setup/plugin_reconcile.py",
            "offset": 280,
            "pattern": "a" * 70,
        },
    },
    {
        "type": "tool_execution_end",
        "toolName": "read",
        "isError": False,
        "durationMs": 12,
    },
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "error",
            "errorMessage": '429 {"message":"rate\nlimited"}\n',
            "content": [],
        },
    },
    {
        "type": "auto_retry_start",
        "attempt": 1,
        "maxAttempts": 3,
        "delayMs": 2000,
        "errorMessage": "429 rate limited",
    },
    {"type": "auto_retry_end", "success": True, "attempt": 2},
    {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_delta",
            "contentIndex": 0,
            "delta": "No ",
        },
    },
    {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_delta",
            "contentIndex": 0,
            "delta": "finding.",
        },
    },
    {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_end",
            "contentIndex": 0,
            "content": "No finding.",
        },
    },
    {
        "type": "message_end",
        "message": {"role": "assistant", "stopReason": "stop", "content": []},
    },
    {"type": "tool_execution_end", "toolName": "grep", "isError": True},
    {"type": "agent_settled", "aborted": False},
    {"type": "pool_result", "entry": "nvidia-glm", "attempt": 1, "outcome": "answered"},
    {"type": "pool_end"},
]

_RENDERED = (
    "[  0.5s] == nvidia-glm (attempt 1) ==\n"
    "[  1.5s] thinking\n"
    "[  2.0s] tool read(path=setup/plugin_reconcile.py, offset=280, pattern="
    + "a"
    * 57
    + "...)\n"
    "[  2.5s]   <- read ok 12 ms\n"
    '[  3.0s] error 429 {"message":"rate limited"}\n'
    "[  3.5s] retry 1/3 in 2000 ms: 429 rate limited\n"
    "[  4.0s] retry succeeded (attempt 2)\n"
    "[  4.5s] text: No finding.\n"
    "[  6.5s]   <- grep error ? ms\n"
    "[  7.0s] settled\n"
    "[  7.5s] -> nvidia-glm attempt 1: answered\n"
    "[  8.0s] pool end\n"
)


def _events_text(records):
    """An events file's text: one JSON object per line, with a broken line in the middle."""
    lines = [json.dumps(r) for r in records]
    lines.insert(3, '{"type": "message_upd')
    return "\n".join(lines) + "\n"


def _view(path, *args):
    """Run the viewer script as a split would."""
    return subprocess.run(
        [sys.executable, "-I", str(Path(pev.__file__)), str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class TestPiEventsView:
    """The split shows one readable line per event, with the time since it started."""

    def test_renderer_output_is_pinned_whole(self):
        ticks = iter(i * 0.5 for i in range(len(_EVENTS)))
        renderer = pev.EventRenderer(lambda: next(ticks))

        shown = "".join(renderer.feed(record) for record in _EVENTS)

        assert shown == _RENDERED
        assert renderer.finished

    def test_the_script_renders_a_file_and_exits_on_pool_end(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text(_events_text(_EVENTS))

        done = _view(path)

        assert done.returncode == 0
        assert done.stderr == ""
        # The script's clock is real: compare the text without its time stamps.
        assert re.sub(r"\[ *\d+\.\ds\]", "[T]", done.stdout) == re.sub(
            r"\[ *\d+\.\ds\]", "[T]", _RENDERED
        )

    def test_it_follows_the_file_as_it_grows_and_reads_a_half_written_line_whole(
        self, tmp_path
    ):
        path = tmp_path / "events.jsonl"
        lines = [json.dumps(r) + "\n" for r in _EVENTS]
        path.write_text(lines[0] + lines[1])
        viewer = subprocess.Popen(
            [sys.executable, "-I", str(Path(pev.__file__)), str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.6)
            with path.open("a") as f:
                f.write("".join(lines[2:6]) + lines[6][:15])
                f.flush()
                time.sleep(0.6)
                f.write(lines[6][15:] + "".join(lines[7:]))
            stdout, stderr = viewer.communicate(timeout=30)
        finally:
            viewer.kill()

        assert (viewer.returncode, stderr) == (0, "")
        assert re.sub(r"\[ *\d+\.\ds\]", "[T]", stdout) == re.sub(
            r"\[ *\d+\.\ds\]", "[T]", _RENDERED
        )

    def test_control_characters_of_event_strings_never_reach_the_terminal(self):
        records = [
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "a\x1b[2Jb\x9bc\nd",
                },
            },
            {"type": "message_update", "assistantMessageEvent": {"type": "text_end"}},
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "args": {"command": "echo \x1b]0;pwn\x07"},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "429 \x1b[31mred\x1b[0m\r",
                },
            },
            {
                "type": "pool_result",
                "entry": "e\x1b",
                "attempt": 1,
                "outcome": "failed: \x1b[31mred\x07",
            },
        ]
        ticks = iter(i * 0.5 for i in range(len(records)))
        renderer = pev.EventRenderer(lambda: next(ticks))

        shown = "".join(renderer.feed(record) for record in records)

        assert shown == (
            "[  0.0s] text: a?[2Jb?c\nd\n"
            "[  1.0s] tool bash(command=echo ?]0;pwn?)\n"
            "[  1.5s] error 429 ?[31mred?[0m\n"
            "[  2.0s] -> e? attempt 1: failed: ?[31mred?\n"
        )
        assert "\x1b" not in shown

    def test_text_reordering_and_invisible_characters_are_replaced_too(self):
        records = [
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "a\u202eb\u200bc\u2028d\u061ce\ufeff",
                },
            },
            {"type": "message_update", "assistantMessageEvent": {"type": "text_end"}},
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "args": {"command": "x\u202ey\u200bz\u2028w\u2066v\u2069"},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "429 \u202e\u200b\u2028 \u2060end",
                },
            },
        ]
        ticks = iter(i * 0.5 for i in range(len(records)))
        renderer = pev.EventRenderer(lambda: next(ticks))

        shown = "".join(renderer.feed(record) for record in records)

        assert shown == (
            "[  0.0s] text: a?b?c?d?e?\n"
            "[  1.0s] tool bash(command=x?y?z?w?v?)\n"
            "[  1.5s] error 429 ??? ?end\n"
        )

    @pytest.mark.parametrize(
        "family",
        [
            pytest.param([0x00AD], id="soft-hyphen"),
            pytest.param([0x034F], id="combining-grapheme-joiner"),
            pytest.param([0x061C], id="arabic-letter-mark"),
            pytest.param([0x180E], id="mongolian-vowel-separator"),
            pytest.param(range(0x200B, 0x2010), id="zero-width-and-marks"),
            pytest.param(range(0x2028, 0x202F), id="separators-and-bidi-embeddings"),
            pytest.param(range(0x2060, 0x2070), id="word-joiner-block"),
            pytest.param(range(0xFE00, 0xFE10), id="variation-selectors"),
            pytest.param(
                range(0x180B, 0x1810), id="mongolian-free-variation-selectors"
            ),
            pytest.param(range(0xE0100, 0xE01F0), id="variation-selectors-supplement"),
            pytest.param([0xFEFF], id="byte-order-mark"),
            pytest.param(range(0xFFF9, 0xFFFC), id="interlinear-annotation"),
            pytest.param([0xE0001], id="language-tag"),
            pytest.param(range(0xE0020, 0xE0080), id="tag-characters"),
            pytest.param(
                [
                    c
                    for c in range(0x00, 0xA0)
                    if c not in (0x0A, 0x09) and not 0x20 <= c < 0x7F
                ],
                id="c0-del-c1",
            ),
        ],
    )
    def test_every_hidden_character_family_is_replaced(self, family):
        text = "".join("a" + chr(c) for c in family)
        record = {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": text},
        }

        shown = pev.EventRenderer(lambda: 0.0).feed(record)

        assert shown == "[  0.0s] text: " + "a?" * len(family)

    def test_the_viewer_keeps_newline_tab_and_visible_text(self):
        record = {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "a\tb\nc\u00e9"},
        }

        shown = pev.EventRenderer(lambda: 0.0).feed(record)

        assert shown == "[  0.0s] text: a\tb\nc\u00e9"

    def test_squashed_fields_replace_nel_and_paragraph_separator_and_keep_nbsp(self):
        record = {
            "type": "tool_execution_start",
            "toolName": "bash",
            "args": {"command": "a\x85b\u2029c\u00a0d  e\r\nf"},
        }

        shown = pev.EventRenderer(lambda: 0.0).feed(record)

        assert shown == "[  0.0s] tool bash(command=a?b?c\u00a0d e f)\n"

    def test_a_character_split_across_two_reads_is_decoded_exactly(self, tmp_path):
        path = tmp_path / "events.jsonl"
        delta = {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "caf\u00e9 \u65e5\u672c",
            },
        }
        raw = (json.dumps(delta, ensure_ascii=False) + "\n").encode("utf-8")
        cut = raw.index("\u65e5".encode("utf-8")) + 1  # inside the 3-byte character
        path.write_bytes(raw[:cut])
        viewer = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(pev.__file__)),
                str(path),
                "--idle-timeout",
                "3",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            time.sleep(0.8)
            with path.open("ab") as f:
                f.write(raw[cut:])
            stdout, stderr = viewer.communicate(timeout=30)
        finally:
            viewer.kill()

        assert (viewer.returncode, stderr) == (0, "")
        assert (
            re.sub(r"\[ *\d+\.\ds\]", "[T]", stdout)
            == "[T] text: caf\u00e9 \u65e5\u672c"
        )

    def test_a_run_that_goes_quiet_ends_the_viewer_after_the_idle_timeout(
        self, tmp_path
    ):
        path = tmp_path / "events.jsonl"
        path.write_text(json.dumps(_EVENTS[1]) + "\n")

        done = _view(path, "--idle-timeout", "0.5")

        assert done.returncode == 0
        assert re.sub(r"\[ *\d+\.\ds\]", "[T]", done.stdout) == (
            "[T] == nvidia-glm (attempt 1) ==\n"
        )

    def test_a_file_that_never_appears_is_said_and_fails(self, tmp_path):
        path = tmp_path / "events.jsonl"

        done = _view(path, "--idle-timeout", "0.5")

        assert (done.returncode, done.stdout, done.stderr) == (
            1,
            "",
            f"no events file {path} within 0.5s\n",
        )


class TestCallScope(unittest.TestCase):
    """A call's `pi` processes are tracked by its scope and refused once the call is cancelled."""

    def test_no_scope_is_no_restriction(self):
        call_scope.check()

    def test_a_cancelled_scope_refuses_a_new_pi_before_it_starts(self):
        scope = call_scope.CallScope()
        scope.cancel()
        with unittest.mock.patch(
            "model_adversarial.Popen", side_effect=AssertionError("pi started")
        ):
            with self.assertRaises(call_scope.CallCancelled):
                call_scope.run_in(scope, ma.call_cli, MISTRAL, "p", 30)

    def test_a_pi_started_as_the_call_is_cancelled_is_killed_and_refused(self):
        scope = call_scope.CallScope()
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(proc.kill)
        scope.cancel()
        with self.assertRaises(call_scope.CallCancelled):
            scope.adopt(proc)
        self.assertIsNotNone(proc.poll())

    def test_a_refused_pi_has_its_pipes_closed(self):
        scope = call_scope.CallScope()
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(proc.kill)
        scope.cancel()
        with self.assertRaises(call_scope.CallCancelled):
            scope.adopt(proc)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    def test_kill_session_takes_down_the_whole_process_group(self):
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 30 & sleep 30"], start_new_session=True
        )
        self.addCleanup(proc.kill)
        time.sleep(0.3)
        call_scope.kill_session(proc)
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except (ProcessLookupError, PermissionError):
                # Groupe disparu : son identifiant peut déjà être celui d'un autre utilisateur.
                return
            time.sleep(0.05)
        self.fail("a member of the process group survived kill_session")

    def test_a_guardian_whose_pi_ended_is_reaped_and_leaves_no_zombie(self):
        listing = Path(self.enterContext(tempfile.TemporaryDirectory())) / "ps.txt"

        def children() -> dict[int, str]:
            """The command of every child of this process, by pid (a zombie keeps its pid)."""
            # Aucun `subprocess` entre la mort du gardien et l'assertion finale : chaque lancement
            # y récolte les sous-processus terminés, le test verrait un gardien récolté par lui
            # et non par le code testé. `-ww` : la commande du gardien est longue.
            ps = os.posix_spawn(
                "/bin/ps",
                ["ps", "-ww", "-A", "-o", "pid=,ppid=,command="],
                os.environ,
                file_actions=[
                    (
                        os.POSIX_SPAWN_OPEN,
                        1,
                        str(listing),
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        0o600,
                    )
                ],
            )
            _, status = os.waitpid(ps, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            fields = [row.split(None, 2) for row in listing.read_text().splitlines()]
            return {
                int(f[0]): f[2] if len(f) > 2 else ""
                for f in fields
                if f[1] == str(os.getpid())
            }

        scope = call_scope.CallScope()
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(proc.kill)
        before = set(children())
        scope.adopt(proc)
        guardians = [
            pid
            for pid, command in children().items()
            if pid not in before and "pi_guardian" in command
        ]
        self.assertIn(proc.pid, children())
        self.assertEqual(len(guardians), 1, children())
        proc.kill()
        proc.wait()

        # Le gardien s'arrête de lui-même une fois son groupe disparu ; tant que personne ne le
        # récolte, son pid reste dans la table des processus.
        deadline = time.monotonic() + 5
        while guardians[0] in children() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertNotIn(guardians[0], children())

    def test_cancel_kills_only_the_processes_of_its_own_scope(self):
        mine, other = call_scope.CallScope(), call_scope.CallScope()
        procs = [
            subprocess.Popen(["sleep", "30"], start_new_session=True) for _ in range(2)
        ]
        for proc in procs:
            self.addCleanup(proc.kill)
        mine.adopt(procs[0])
        other.adopt(procs[1])
        mine.cancel()
        procs[0].wait(timeout=5)
        self.assertIsNone(procs[1].poll())

    def test_a_worker_thread_runs_under_the_scope_it_was_bound_in(self):
        scope = call_scope.CallScope()
        scope.cancel()

        def in_worker_thread():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(call_scope.bind(call_scope.check)).result()

        with self.assertRaises(call_scope.CallCancelled):
            call_scope.run_in(scope, in_worker_thread)

    def test_cancel_reports_each_killed_pi_with_call_id_pid_and_reason(self):
        scope = call_scope.CallScope(call_id="42")
        procs = [
            subprocess.Popen(["sleep", "30"], start_new_session=True) for _ in range(2)
        ]
        for proc in procs:
            self.addCleanup(proc.kill)
            scope.adopt(proc)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scope.cancel("wall-cap")
        for proc in procs:
            proc.wait(timeout=5)
        # Une ligne par pi tué, rien d'autre qu'id, pid et motif — pas de valeur de clé.
        self.assertEqual(
            sorted(err.getvalue().splitlines()),
            sorted(
                f"pc-core: call id=42 pi killed pid={proc.pid} reason=wall-cap"
                for proc in procs
            ),
        )

    def test_a_pi_refused_after_the_cancel_is_reported_with_the_same_reason(self):
        scope = call_scope.CallScope(call_id="7")
        scope.cancel("wall-cap")
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(proc.kill)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(call_scope.CallCancelled):
                scope.adopt(proc)
        proc.wait(timeout=5)
        self.assertEqual(
            err.getvalue().splitlines(),
            [f"pc-core: call id=7 pi killed pid={proc.pid} reason=wall-cap"],
        )

    def test_a_scope_that_ends_normally_logs_no_kill(self):
        scope = call_scope.CallScope(call_id="9")
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait()
        scope.adopt(proc)
        scope.release(proc)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scope.cancel("closed-channel")
        self.assertEqual(err.getvalue(), "")

    def test_cancel_twice_reports_each_pi_once(self):
        scope = call_scope.CallScope(call_id="1")
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(proc.kill)
        scope.adopt(proc)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scope.cancel("cancelled")
            scope.cancel("wall-cap")
        proc.wait(timeout=5)
        self.assertEqual(
            err.getvalue().splitlines(),
            [f"pc-core: call id=1 pi killed pid={proc.pid} reason=cancelled"],
        )


class TestCallTimeout(unittest.TestCase):
    """The wall-clock cap a served call runs under, from `PC_CORE_CALL_TIMEOUT_S`."""

    def test_the_cap_defaults_to_1800s(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PC_CORE_CALL_TIMEOUT_S", None)
            self.assertEqual(call_scope.call_timeout(), 1800.0)

    def test_the_cap_reads_the_variable(self):
        with unittest.mock.patch.dict(os.environ, {"PC_CORE_CALL_TIMEOUT_S": "30"}):
            self.assertEqual(call_scope.call_timeout(), 30.0)

    def test_an_unreadable_or_non_positive_cap_falls_back_to_the_default(self):
        for raw in ("", "soon", "0", "-5"):
            with unittest.mock.patch.dict(os.environ, {"PC_CORE_CALL_TIMEOUT_S": raw}):
                self.assertEqual(call_scope.call_timeout(), 1800.0, raw)


# Les quatre outils qui lancent `pi`, avec des arguments et le nombre de `pi` qu'un appel lance
# d'un coup dans un pool de deux entrées.
_PI_TOOLS = {
    "adversarial": ({"task": "attack this"}, 1),
    "panel": ({"tasks": ["a", "b"]}, 2),
    "pool_check": ({}, 2),
    "offline": ({"task": "summarize"}, 1),
}


@pytest.fixture
def make_server(tmp_path):
    """A factory of real `pc_core_server.py` subprocesses with a fake `pi`, all stopped at the end."""
    pytest.importorskip("mcp")
    servers: list[PoolServer] = []

    def make(pool_size: int = 2, extra_env: dict[str, str] | None = None) -> PoolServer:
        root = tmp_path / f"server-{len(servers)}"
        root.mkdir()
        server = PoolServer(root, pool_size, extra_env)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


class TestToolSchema:
    """The wrapper that serves each tool must not change what a client sees of it."""

    def test_each_tool_advertises_the_parameters_of_its_function(self, make_server):
        pc_core_server = pytest.importorskip("pc_core_server")
        advertised = {
            tool["name"]: tool["inputSchema"] for tool in make_server().list_tools()
        }

        assert set(advertised) == set(_PI_TOOLS)
        for name, schema in advertised.items():
            parameters = inspect.signature(getattr(pc_core_server, name)).parameters
            required = {n for n, p in parameters.items() if p.default is p.empty}
            assert set(schema["properties"]) == set(parameters)
            assert set(schema.get("required", [])) == required


@pytest.mark.parametrize("tool", _PI_TOOLS)
class TestClientGivingUpKillsPi:
    """The client cancels a call or goes away: the `pi` it was waiting for must not outlive it."""

    def test_a_cancelled_call_kills_each_pi_and_its_child_within_5s(
        self, make_server, tool
    ):
        arguments, expected = _PI_TOOLS[tool]
        server = make_server()
        request = server.start_call(tool, arguments)
        pids = [pid for pair in server.wait_launched(expected) for pid in pair]

        server.cancel(request)

        assert server.wait_gone(pids, 5)
        time.sleep(0.5)
        server.drain()
        assert request not in server.answered

    def test_a_closed_channel_kills_every_pi_and_the_server_exits(
        self, make_server, tool
    ):
        arguments, expected = _PI_TOOLS[tool]
        server = make_server()
        server.start_call(tool, arguments)
        pids = [pid for pair in server.wait_launched(expected) for pid in pair]

        started = time.monotonic()
        server.close_stdin()

        assert server.wait_gone(pids, 5)
        assert server.wait_exit(5 - (time.monotonic() - started))

    def test_a_killed_server_leaves_no_pi_within_5s(self, make_server, tool):
        arguments, expected = _PI_TOOLS[tool]
        server = make_server()
        server.start_call(tool, arguments)
        pids = [pid for pair in server.wait_launched(expected) for pid in pair]

        server.process.kill()

        assert server.wait_gone(pids, 5)


class TestCancellationScope:
    """What a cancellation leaves alone, or never starts."""

    def test_a_cancelled_adversarial_call_starts_no_pi_of_its_second_entry(
        self, make_server
    ):
        server = make_server(pool_size=2)
        request = server.start_call("adversarial", {"task": "attack this"})
        pids = [pid for pair in server.wait_launched(1) for pid in pair]

        server.cancel(request)

        assert server.wait_gone(pids, 5)
        time.sleep(1)
        assert server.launch_count() == 1
        assert server.wait_gone(server.all_pids(), 1)

    def test_a_cancelled_pool_check_starts_no_pi_for_the_entries_still_queued(
        self, make_server
    ):
        server = make_server(pool_size=ma.CHECK_WORKERS + 2)
        request = server.start_call("pool_check", {})
        pids = [pid for pair in server.wait_launched(ma.CHECK_WORKERS) for pid in pair]

        server.cancel(request)

        assert server.wait_gone(pids, 5)
        time.sleep(1)
        assert server.launch_count() == ma.CHECK_WORKERS
        assert server.wait_gone(server.all_pids(), 1)

    def test_cancelling_one_call_spares_another_call_and_another_session(
        self, make_server
    ):
        session = make_server()
        other_session = make_server()
        cancelled = session.start_call("adversarial", {"task": "t", "model": "fake-0"})
        session.start_call("adversarial", {"task": "t", "model": "fake-1"})
        other_session.start_call("adversarial", {"task": "t", "model": "fake-0"})
        session.wait_launched(2)
        other_session.wait_launched(1)

        session.cancel(cancelled)

        assert session.wait_gone(session.pids_of("model-0"), 5)
        time.sleep(1)
        spared = session.pids_of("model-1") + other_session.pids_of("model-0")
        assert len(spared) == 4
        assert all(alive(pid) for pid in spared)


CAP_S = 3


class TestWallCap:
    """The client stays alive with the channel open and never cancels: the cap alone must end
    the call and take its `pi` down."""

    def test_a_call_nobody_cancels_is_killed_by_the_cap_within_5s(self, make_server):
        server = make_server(extra_env={"PC_CORE_CALL_TIMEOUT_S": str(CAP_S)})
        request = server.start_call("adversarial", {"task": "attack this"})
        pids = [pid for pair in server.wait_launched(1) for pid in pair]

        # Avant le cap : le client est vivant, le canal ouvert, le pi et son enfant aussi.
        assert all(alive(pid) for pid in pids)

        assert server.wait_gone(pids, CAP_S + 5)
        assert server.wait_answered(request, 5)

    def test_the_kill_log_names_the_call_the_pi_and_the_cap(self, make_server):
        server = make_server(extra_env={"PC_CORE_CALL_TIMEOUT_S": str(CAP_S)})
        request = server.start_call("adversarial", {"task": "SECRET-ARG-VALUE"})
        pairs = server.wait_launched(1)
        pids = [pid for pair in pairs for pid in pair]

        assert server.wait_gone(pids, CAP_S + 5)
        deadline = time.monotonic() + 5
        log = server.stderr_text()
        while time.monotonic() < deadline and "reason=wall-cap" not in log:
            time.sleep(0.1)
            log = server.stderr_text()
        assert f"call id={request} cancelled reason=wall-cap" in log
        # Le pi tué, pas son enfant : celui-là meurt avec le groupe, il n'est pas lancé par le serveur.
        for pi, _ in pairs:
            assert f"call id={request} pi killed pid={pi} reason=wall-cap" in log
        # Allowlist de champs : la valeur d'un argument d'appel n'atteint jamais le journal.
        assert "SECRET-ARG-VALUE" not in log

    def test_a_received_cancellation_is_logged_with_its_call_id(self, make_server):
        server = make_server()
        request = server.start_call("adversarial", {"task": "attack this"})
        pairs = server.wait_launched(1)
        pids = [pid for pair in pairs for pid in pair]

        server.cancel(request)
        assert server.wait_gone(pids, 5)

        deadline = time.monotonic() + 5
        log = server.stderr_text()
        while time.monotonic() < deadline and "cancel received" not in log:
            time.sleep(0.1)
            log = server.stderr_text()
        assert f"call id={request} cancel received" in log
        for pi, _ in pairs:
            assert f"pi killed pid={pi}" in log


if __name__ == "__main__":
    unittest.main()
