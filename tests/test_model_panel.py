#!/usr/bin/env python3
"""Tests for plugins/pc-core/mcp/model_panel.py — run with:
python3 -m unittest discover tests
"""

import sys
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import model_panel as mp

MISTRAL = {
    "name": "mistral-medium-3.5",
    "kind": "http",
    "provider": "mistral",
    "model": "mistral-medium-3.5",
    "base_url": "https://api.mistral.ai/v1/chat/completions",
    "api_key_env": "MISTRAL_API_KEY",  # pragma: allowlist secret
    "capacity_tier": "opus",
    "free_tier": True,
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
    "provider": "openrouter",  # same family as GLM — only one of the two may enter a panel
    "model": "moonshotai/kimi-k2.6:free",
    "base_url": "https://openrouter.ai/api/v1/chat/completions",
    "api_key_env": "OPENROUTER_API_KEY",  # pragma: allowlist secret
    "capacity_tier": "sonnet",
    "free_tier": True,
    "concurrency": None,
}
GEMINI = {
    "name": "gemini-3.6-flash",
    "kind": "http",
    "provider": "gemini",
    "model": "gemini-3.6-flash",
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "api_key_env": "GEMINI_API_KEY",  # pragma: allowlist secret
    "capacity_tier": "sonnet",
    "free_tier": True,
    "concurrency": None,
}
COHERE_R7B = {
    "name": "cohere-command-r7b",
    "kind": "http",
    "provider": "cohere",
    "model": "command-r7b-12-2024",
    "base_url": "https://api.cohere.com/compatibility/v1/chat/completions",
    "api_key_env": "COHERE_API_KEY",  # pragma: allowlist secret
    "capacity_tier": "haiku",  # too weak for an advisor slot — must never be picked
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
POOL = [MISTRAL, GLM, KIMI, GEMINI, COHERE_R7B, OFFLINE]


class TestSelectForPanel(unittest.TestCase):
    def test_picks_distinct_families_strongest_tier_first_then_by_name(self):
        # opus (mistral, glm) before sonnet (gemini); glm wins openrouter over sonnet-tier kimi;
        # cohere-r7b (haiku) and local-ollama (offline) never appear
        self.assertEqual(mp.select_for_panel(POOL, n=5), [GLM, MISTRAL, GEMINI])

    def test_n_caps_the_result(self):
        self.assertEqual(mp.select_for_panel(POOL, n=2), [GLM, MISTRAL])

    def test_excludes_offline_and_haiku_tier(self):
        picked = mp.select_for_panel(POOL, n=99)
        self.assertNotIn(OFFLINE, picked)
        self.assertNotIn(COHERE_R7B, picked)

    def test_one_entry_per_provider(self):
        picked = mp.select_for_panel(POOL, n=99)
        providers = [e["provider"] for e in picked]
        self.assertEqual(len(providers), len(set(providers)))

    def test_deterministic_across_calls(self):
        self.assertEqual(mp.select_for_panel(POOL, n=5), mp.select_for_panel(POOL, n=5))

    def test_explicit_models_keep_order_drop_ineligible_and_dedupe_family(self):
        # requested order kept; cohere-r7b (haiku) and unknown name dropped; glm-5-2 is the same
        # openrouter family as kimi (already taken) → dropped too
        self.assertEqual(
            mp.select_for_panel(
                POOL, n=5, models=["kimi-k2.6", "cohere-command-r7b", "nope", "glm-5-2"]
            ),
            [KIMI],
        )

    def test_explicit_models_respect_n(self):
        self.assertEqual(
            mp.select_for_panel(POOL, n=1, models=["glm-5-2", "mistral-medium-3.5"]),
            [GLM],
        )

    def test_empty_pool_yields_empty_list(self):
        self.assertEqual(mp.select_for_panel([], n=5), [])

    def test_tiers_restricts_selection_to_the_named_capacity_tier(self):
        # explicit opt-in past the default sonnet/opus advisor filter — cohere-r7b is the only
        # haiku-tier http entry; the opus/sonnet models are now out
        self.assertEqual(mp.select_for_panel(POOL, n=5, tiers={"haiku"}), [COHERE_R7B])

    def test_tiers_still_excludes_offline_entries(self):
        # haiku is allowed now, but local-ollama is offline — it has its own zero-egress tool
        self.assertNotIn(OFFLINE, mp.select_for_panel(POOL, n=5, tiers={"haiku"}))

    def test_tiers_applies_on_the_explicit_models_path_too(self):
        self.assertEqual(
            mp.select_for_panel(
                POOL, n=5, models=["cohere-command-r7b", "glm-5-2"], tiers={"haiku"}
            ),
            [COHERE_R7B],
        )

    def test_tiers_accepts_a_bare_string(self):
        self.assertEqual(mp.select_for_panel(POOL, n=5, tiers="haiku"), [COHERE_R7B])


class TestRunPanel(unittest.TestCase):
    def _fake_provider(self, answers_by_model):
        def fake_call_provider(
            base_url, api_key, model, prompt, timeout=180, max_tokens=mp.ma.MAX_TOKENS
        ):
            if isinstance(answers_by_model.get(model), Exception):
                raise answers_by_model[model]
            return {"choices": [{"message": {"content": answers_by_model[model]}}]}

        return fake_call_provider

    def test_answers_come_back_in_entry_order_tagged_with_their_slot(self):
        with (
            unittest.mock.patch.object(
                mp.ma,
                "call_provider",
                self._fake_provider(
                    {
                        "z-ai/glm-5-2": "glm says",
                        "mistral-medium-3.5": "mistral says",
                        "gemini-3.6-flash": "gemini says",
                    }
                ),
            ),
            unittest.mock.patch.object(mp.ma, "resolve_api_key_for", return_value="k"),
        ):
            resp = mp.run_panel([GLM, MISTRAL, GEMINI], ["p1", "p2", "p3"])
        self.assertEqual(
            resp.answers,
            [
                mp.Answer(1, "glm-5-2", "glm says"),
                mp.Answer(2, "mistral-medium-3.5", "mistral says"),
                mp.Answer(3, "gemini-3.6-flash", "gemini says"),
            ],
        )
        self.assertEqual(resp.skipped, [])

    def test_middle_slot_skipped_keeps_the_slot_numbers_of_the_survivors(self):
        def key_for(entry):
            return None if entry["name"] == "mistral-medium-3.5" else "k"

        with (
            unittest.mock.patch.object(
                mp.ma,
                "call_provider",
                self._fake_provider(
                    {"z-ai/glm-5-2": "glm", "gemini-3.6-flash": "gemini"}
                ),
            ),
            unittest.mock.patch.object(
                mp.ma, "resolve_api_key_for", side_effect=key_for
            ),
        ):
            resp = mp.run_panel([GLM, MISTRAL, GEMINI], ["p1", "p2", "p3"])
        # slot 2 died; survivors keep slots 1 and 3, not 1 and 2 — the caller maps slot -> lens
        self.assertEqual(
            resp.answers,
            [
                mp.Answer(1, "glm-5-2", "glm"),
                mp.Answer(3, "gemini-3.6-flash", "gemini"),
            ],
        )
        self.assertEqual(resp.skipped[0].slot, 2)
        self.assertEqual(resp.skipped[0].model, "mistral-medium-3.5")
        self.assertIn("no API key", resp.skipped[0].reason)

    def test_timeout_is_a_skipped_slot_with_no_retry(self):
        calls: list[str] = []

        def fake_call_provider(
            base_url, api_key, model, prompt, timeout=180, max_tokens=mp.ma.MAX_TOKENS
        ):
            calls.append(model)
            raise TimeoutError("timed out")

        with (
            unittest.mock.patch.object(mp.ma, "call_provider", fake_call_provider),
            unittest.mock.patch.object(mp.ma, "resolve_api_key_for", return_value="k"),
        ):
            resp = mp.run_panel([GLM], ["p1"])
        self.assertEqual(resp.answers, [])
        self.assertEqual(resp.skipped[0].slot, 1)
        self.assertEqual(resp.skipped[0].model, "glm-5-2")
        self.assertIn("TimeoutError", resp.skipped[0].reason)
        self.assertEqual(calls, ["z-ai/glm-5-2"])  # tried once, no retry

    def test_http_error_from_one_model_does_not_sink_the_batch(self):
        with (
            unittest.mock.patch.object(
                mp.ma,
                "call_provider",
                self._fake_provider(
                    {
                        "z-ai/glm-5-2": urllib.error.HTTPError(
                            "u",
                            429,
                            "rate",
                            {},
                            None,  # type: ignore[arg-type]
                        ),
                        "mistral-medium-3.5": "ok",
                    }
                ),
            ),
            unittest.mock.patch.object(mp.ma, "resolve_api_key_for", return_value="k"),
        ):
            resp = mp.run_panel([GLM, MISTRAL], ["p1", "p2"])
        self.assertEqual(resp.answers, [mp.Answer(2, "mistral-medium-3.5", "ok")])
        self.assertEqual(resp.skipped, [mp.Skip(1, "glm-5-2", "HTTP 429")])

    def test_offline_entry_slipping_through_is_skipped_never_called(self):
        with unittest.mock.patch.object(mp.ma, "call_provider") as cp:
            resp = mp.run_panel([OFFLINE], ["p1"])
        cp.assert_not_called()
        self.assertEqual(resp.answers, [])
        self.assertEqual(resp.skipped[0].slot, 1)
        self.assertIn("offline", resp.skipped[0].reason)

    def test_no_entries_yields_empty_response(self):
        resp = mp.run_panel([], [])
        self.assertEqual(resp, mp.PanelResponse([], []))

    def test_entries_prompts_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            mp.run_panel([GLM, MISTRAL], ["only one prompt"])

    def test_each_slot_gets_its_own_prompt(self):
        seen: dict[str, str] = {}

        def fake_call_provider(
            base_url, api_key, model, prompt, timeout=180, max_tokens=mp.ma.MAX_TOKENS
        ):
            seen[model] = prompt
            return {"choices": [{"message": {"content": "ok"}}]}

        with (
            unittest.mock.patch.object(mp.ma, "call_provider", fake_call_provider),
            unittest.mock.patch.object(mp.ma, "resolve_api_key_for", return_value="k"),
        ):
            mp.run_panel([GLM, MISTRAL], ["lens A prompt", "lens B prompt"])
        self.assertEqual(seen["z-ai/glm-5-2"], "lens A prompt")
        self.assertEqual(seen["mistral-medium-3.5"], "lens B prompt")


class TestFormatPanel(unittest.TestCase):
    def test_all_slots_selected_one_failed(self):
        # 3 requested, 3 selected, slot 3 died → no "unfilled", one "skipped"; blocks carry the slot
        resp = mp.PanelResponse(
            answers=[
                mp.Answer(1, "glm-5-2", "A"),
                mp.Answer(2, "mistral-medium-3.5", "B"),
            ],
            skipped=[mp.Skip(3, "gemini-3.6-flash", "HTTP 500")],
        )
        self.assertEqual(
            mp.format_panel(resp, requested=3),
            "[panel] 2/3 slots answered · skipped: slot 3 gemini-3.6-flash (HTTP 500)\n\n"
            "--- [slot 1 · glm-5-2] ---\nA\n\n"
            "--- [slot 2 · mistral-medium-3.5] ---\nB",
        )

    def test_unfilled_slots_are_named_and_distinguished_from_failed_ones(self):
        # 5 requested, only 2 families selected (both answered), 0 failed → slots 3,4,5 unfilled
        resp = mp.PanelResponse(
            answers=[
                mp.Answer(1, "glm-5-2", "A"),
                mp.Answer(2, "mistral-medium-3.5", "B"),
            ],
            skipped=[],
        )
        self.assertEqual(
            mp.format_panel(resp, requested=5),
            "[panel] 2/5 slots answered · "
            "unfilled: slot(s) 3, 4, 5 — pool has fewer eligible families than requested\n\n"
            "--- [slot 1 · glm-5-2] ---\nA\n\n--- [slot 2 · mistral-medium-3.5] ---\nB",
        )

    def test_both_a_failure_and_an_unfilled_slot(self):
        resp = mp.PanelResponse(
            answers=[mp.Answer(1, "glm-5-2", "A")],
            skipped=[mp.Skip(2, "mistral-medium-3.5", "HTTP 429")],
        )
        self.assertEqual(
            mp.format_panel(resp, requested=4),
            "[panel] 1/4 slots answered · skipped: slot 2 mistral-medium-3.5 (HTTP 429) · "
            "unfilled: slot(s) 3, 4 — pool has fewer eligible families than requested\n\n"
            "--- [slot 1 · glm-5-2] ---\nA",
        )

    def test_header_only_when_nothing_answered(self):
        resp = mp.PanelResponse(answers=[], skipped=[mp.Skip(1, "glm-5-2", "HTTP 401")])
        self.assertEqual(
            mp.format_panel(resp, requested=1),
            "[panel] 0/1 slots answered · skipped: slot 1 glm-5-2 (HTTP 401)",
        )


if __name__ == "__main__":
    unittest.main()
