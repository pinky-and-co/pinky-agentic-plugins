#!/usr/bin/env python3
"""Tests for plugins/pc-core/mcp/model_adversarial.py — run with:
python3 -m unittest discover tests
"""

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import model_adversarial as ma

MISTRAL = {
    "name": "mistral-medium-3.5",
    "kind": "http",
    "provider": "mistral",
    "model": "mistral-medium-3.5",
    "base_url": "https://api.mistral.ai/v1/chat/completions",
    "api_key_env": "MISTRAL_API_KEY",  # pragma: allowlist secret
    "capacity_tier": "opus",
    "free_tier": False,
    "concurrency": None,
}
GLM = {
    "name": "glm-5-2",
    "kind": "http",
    "provider": "openrouter",
    "model": "z-ai/glm-5-2",
    "base_url": "https://openrouter.ai/api/v1/chat/completions",
    "api_key_env": "OPENROUTER_API_KEY",  # pragma: allowlist secret
    "capacity_tier": "opus",
    "free_tier": True,
    "concurrency": None,
}
KIMI = {
    "name": "kimi-k2.6",
    "kind": "http",
    "provider": "openrouter",
    "model": "moonshotai/kimi-k2.6:free",
    "base_url": "https://openrouter.ai/api/v1/chat/completions",
    "api_key_env": "OPENROUTER_API_KEY",  # pragma: allowlist secret
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
    "base_url": "https://openrouter.ai/api/v1/chat/completions",
    "api_key_env": "OPENROUTER_API_KEY",  # pragma: allowlist secret
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
        custom_pool = [{"name": "custom", "kind": "http", "capacity_tier": "haiku"}]
        (self.project_dir / ".claude").mkdir()
        (self.project_dir / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"model_pool": custom_pool})
        )
        self.assertEqual(ma.load_pool(), custom_pool)


class TestResolveApiKeyFor(unittest.TestCase):
    def test_env_var_wins_and_keychain_is_not_consulted(self):
        import os

        os.environ["OPENROUTER_API_KEY"] = "env-value"  # pragma: allowlist secret
        try:
            with unittest.mock.patch.object(ma, "read_macos_keychain") as kc:
                self.assertEqual(ma.resolve_api_key_for(GLM), "env-value")
            kc.assert_not_called()
        finally:
            del os.environ["OPENROUTER_API_KEY"]

    def test_falls_back_to_keychain_keyed_on_the_env_name(self):
        import os

        os.environ.pop("OPENROUTER_API_KEY", None)
        with unittest.mock.patch.object(
            ma, "read_macos_keychain", return_value="kc-value"
        ) as kc:
            self.assertEqual(ma.resolve_api_key_for(GLM), "kc-value")
        kc.assert_called_once_with("OPENROUTER_API_KEY")

    def test_neither_env_nor_keychain_returns_none(self):
        import os

        os.environ.pop("OPENROUTER_API_KEY", None)
        with unittest.mock.patch.object(ma, "read_macos_keychain", return_value=None):
            self.assertIsNone(ma.resolve_api_key_for(GLM))


class TestRunPool(unittest.TestCase):
    def test_offline_entry_never_reaches_call_provider(self):
        # only offline in the pool, nothing else to try → must fail without calling call_provider
        result = ma.run_pool([OFFLINE], "prompt")
        self.assertIsNone(result.entry)
        self.assertIn("offline entries are not callable", result.result)

    def test_non_https_base_url_is_refused(self):
        with self.assertRaises(ValueError):
            ma.call_provider(
                "http://insecure.example/v1/chat/completions", "key", "m", "prompt"
            )

    def test_entry_max_tokens_overrides_the_global_default(self):
        captured = {}

        def fake_call_provider(
            base_url, api_key, model, prompt, timeout=180, max_tokens=ma.MAX_TOKENS
        ):
            captured["max_tokens"] = max_tokens
            return {"choices": [{"message": {"content": "ok"}}]}

        entry = {**MISTRAL, "max_tokens": 50}
        with (
            unittest.mock.patch.object(ma, "call_provider", fake_call_provider),
            unittest.mock.patch.object(ma, "resolve_api_key_for", return_value="k"),
        ):
            ma.run_pool([entry], "prompt")
        self.assertEqual(captured["max_tokens"], 50)

    def test_missing_max_tokens_falls_back_to_global_default(self):
        captured = {}

        def fake_call_provider(
            base_url, api_key, model, prompt, timeout=180, max_tokens=ma.MAX_TOKENS
        ):
            captured["max_tokens"] = max_tokens
            return {"choices": [{"message": {"content": "ok"}}]}

        with (
            unittest.mock.patch.object(ma, "call_provider", fake_call_provider),
            unittest.mock.patch.object(ma, "resolve_api_key_for", return_value="k"),
        ):
            ma.run_pool([MISTRAL], "prompt")
        self.assertEqual(captured["max_tokens"], ma.MAX_TOKENS)


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

    def fake_run_pool(entries, prompt, timeout=180):
        captured["prompt"] = prompt
        return ma.PoolResult(entries[0], {"choices": [{"message": {"content": "ok"}}]})

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
        lambda entries, prompt, timeout=180: ma.PoolResult(
            entries[0], {"choices": [{"message": {"content": "ok"}}]}
        ),
    )

    server.adversarial(task="review", tier="turbo-9000")
    assert "adversarial" in seen and "delegation" not in seen


class TestKeylessPoolDegrades(unittest.TestCase):
    def test_all_keyless_entries_yield_none_not_raise(self):
        with unittest.mock.patch.object(ma, "resolve_api_key_for", return_value=None):
            result = ma.run_pool([MISTRAL, GLM], "prompt")
        self.assertIsNone(result.entry)
        self.assertIn("no API key", result.result)


if __name__ == "__main__":
    unittest.main()
