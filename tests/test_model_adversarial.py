#!/usr/bin/env python3
"""Tests for plugins/pc-core/mcp/model_adversarial.py — run with:
python3 -m unittest discover tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import model_adversarial as ma

MISTRAL = {
    "name": "mistral-medium-3.5",
    "kind": "http",
    "provider": "mistral",
    "model": "mistral-medium-3.5",
    "base_url": "https://api.mistral.ai/v1/chat/completions",
    "api_key_env": "MISTRAL_API_KEY",  # pragma: allowlist secret
    "api_key_file": "~/.mistral_api_key",  # pragma: allowlist secret
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
    "api_key_file": "~/.openrouter_api_key",  # pragma: allowlist secret
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
    "api_key_file": "~/.openrouter_api_key",  # pragma: allowlist secret
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
    def test_env_var_wins_over_file(self, monkeypatch=None):
        import os

        os.environ["OPENROUTER_API_KEY"] = "env-value"  # pragma: allowlist secret
        try:
            self.assertEqual(ma.resolve_api_key_for(GLM), "env-value")
        finally:
            del os.environ["OPENROUTER_API_KEY"]

    def test_falls_back_to_file(self):
        import os
        import tempfile

        os.environ.pop("OPENROUTER_API_KEY", None)
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as f:
            f.write("file-value\n")
            key_path = f.name
        try:
            entry = {**GLM, "api_key_file": key_path}
            self.assertEqual(ma.resolve_api_key_for(entry), "file-value")
        finally:
            Path(key_path).unlink()

    def test_neither_present_returns_none(self):
        import os

        os.environ.pop("OPENROUTER_API_KEY", None)
        entry = {**GLM, "api_key_file": "/nonexistent/.openrouter_api_key"}
        self.assertIsNone(ma.resolve_api_key_for(entry))


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


if __name__ == "__main__":
    unittest.main()
