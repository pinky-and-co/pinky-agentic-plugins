#!/usr/bin/env python3
"""Tests for plugins/pc-models/mcp/model_panel.py — run with:
python3 -m unittest discover tests
"""

import json
import os
import re
import sys
import tempfile
import unittest
import subprocess
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-models" / "mcp"))

import model_panel as mp

MISTRAL = {
    "name": "mistral-medium-3.5",
    "kind": "cli",
    "provider": "mistral",
    "model": "mistral-medium-3.5",
    "capacity_tier": "opus",
    "free_tier": True,
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
    "provider": "openrouter",  # same family as GLM — only one of the two may enter a panel
    "model": "moonshotai/kimi-k2.6:free",
    "capacity_tier": "sonnet",
    "free_tier": True,
    "concurrency": None,
}
GEMINI = {
    "name": "gemini-3.6-flash",
    "kind": "cli",
    "provider": "gemini",
    "model": "gemini-3.6-flash",
    "capacity_tier": "sonnet",
    "free_tier": True,
    "concurrency": None,
}
COHERE_R7B = {
    "name": "cohere-command-r7b",
    "kind": "cli",
    "provider": "cohere",
    "model": "command-r7b-12-2024",
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
        # haiku-tier entry; the opus/sonnet models are now out
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
    def _fake_cli(self, answers_by_model, seen=None):
        def fake_call_cli(entry, prompt, timeout, tools=None, env=None, events=None):
            if seen is not None:
                seen[entry["model"]] = (prompt, tools)
            answer = answers_by_model[entry["model"]]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return fake_call_cli

    def test_answers_come_back_in_entry_order_tagged_with_their_slot(self):
        with unittest.mock.patch.object(
            mp.ma,
            "call_cli",
            self._fake_cli(
                {
                    "z-ai/glm-5-2": "glm says",
                    "mistral-medium-3.5": "mistral says",
                    "gemini-3.6-flash": "gemini says",
                }
            ),
        ):
            resp = mp.run_panel([GLM, MISTRAL, GEMINI], ["p1", "p2", "p3"], tools=[])
        self.assertEqual(
            resp.answers,
            [
                mp.Answer(1, "glm-5-2", "glm says", "openrouter/glm-5-2 · 🟧 opus"),
                mp.Answer(
                    2,
                    "mistral-medium-3.5",
                    "mistral says",
                    "mistral/mistral-medium-3.5 · 🟧 opus",
                ),
                mp.Answer(
                    3,
                    "gemini-3.6-flash",
                    "gemini says",
                    "gemini/gemini-3.6-flash · 🟪 sonnet",
                ),
            ],
        )
        self.assertEqual(resp.skipped, [])

    def test_middle_slot_skipped_keeps_the_slot_numbers_of_the_survivors(self):
        failure = subprocess.CalledProcessError(1, "pi", "", "401 unauthorized\n")
        with unittest.mock.patch.object(
            mp.ma,
            "call_cli",
            self._fake_cli(
                {
                    "z-ai/glm-5-2": "glm",
                    "mistral-medium-3.5": failure,
                    "gemini-3.6-flash": "gemini",
                }
            ),
        ):
            resp = mp.run_panel([GLM, MISTRAL, GEMINI], ["p1", "p2", "p3"], tools=[])
        # le slot 2 est mort ; les survivants gardent 1 et 3 — l'appelant associe slot -> lentille
        self.assertEqual(
            resp.answers,
            [
                mp.Answer(1, "glm-5-2", "glm", "openrouter/glm-5-2 · 🟧 opus"),
                mp.Answer(
                    3,
                    "gemini-3.6-flash",
                    "gemini",
                    "gemini/gemini-3.6-flash · 🟪 sonnet",
                ),
            ],
        )
        self.assertEqual(
            resp.skipped,
            [mp.Skip(2, "mistral-medium-3.5", "pi exit 1: 401 unauthorized")],
        )

    def test_timeout_is_a_skipped_slot_with_no_retry(self):
        seen: dict = {}
        with unittest.mock.patch.object(
            mp.ma,
            "call_cli",
            self._fake_cli(
                {"z-ai/glm-5-2": subprocess.TimeoutExpired("pi", 180)}, seen
            ),
        ):
            resp = mp.run_panel([GLM], ["p1"], tools=[])
        self.assertEqual(resp.answers, [])
        self.assertEqual(resp.skipped, [mp.Skip(1, "glm-5-2", "no answer within 180s")])
        self.assertEqual(list(seen), ["z-ai/glm-5-2"])  # une seule tentative

    def test_empty_answer_is_a_skipped_slot(self):
        with unittest.mock.patch.object(
            mp.ma, "call_cli", self._fake_cli({"z-ai/glm-5-2": "  \n"})
        ):
            resp = mp.run_panel([GLM], ["p1"], tools=[])
        self.assertEqual(resp.answers, [])
        self.assertEqual(resp.skipped, [mp.Skip(1, "glm-5-2", "empty answer")])

    def test_the_panel_keeps_its_own_180_seconds(self):
        seen: dict = {}

        def fake_call_cli(entry, prompt, timeout, tools=None, env=None, events=None):
            seen["timeout"] = timeout
            return "answer"

        with unittest.mock.patch.object(mp.ma, "call_cli", fake_call_cli):
            mp.run_panel([GLM], ["p1"], tools=[])
        self.assertEqual(seen["timeout"], 180)

    def test_entry_timeout_is_passed_and_reported(self):
        seen: dict = {}

        def fake_call_cli(entry, prompt, timeout, tools=None, env=None, events=None):
            seen["timeout"] = timeout
            raise subprocess.TimeoutExpired("pi", timeout)

        with unittest.mock.patch.object(mp.ma, "call_cli", fake_call_cli):
            resp = mp.run_panel([{**GLM, "timeout": 300}], ["p1"], tools=[])
        self.assertEqual(seen["timeout"], 300)
        self.assertEqual(resp.skipped, [mp.Skip(1, "glm-5-2", "no answer within 300s")])

    def test_http_entry_is_skipped_never_raised(self):
        http_entry = {**MISTRAL, "kind": "http"}
        resp = mp.run_panel([http_entry], ["p1"], tools=[])
        self.assertEqual(resp.answers, [])
        self.assertEqual(resp.skipped[0].slot, 1)
        self.assertIn("kind 'http' is no longer supported", resp.skipped[0].reason)

    def test_offline_entry_slipping_through_is_skipped_never_called(self):
        with unittest.mock.patch.object(mp.ma, "call_cli") as cc:
            resp = mp.run_panel([OFFLINE], ["p1"], tools=[])
        cc.assert_not_called()
        self.assertEqual(resp.answers, [])
        self.assertEqual(resp.skipped[0].slot, 1)
        self.assertIn("offline", resp.skipped[0].reason)

    def test_no_entries_yields_empty_response(self):
        resp = mp.run_panel([], [], tools=[])
        self.assertEqual(resp, mp.PanelResponse([], []))

    def test_entries_prompts_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            mp.run_panel([GLM, MISTRAL], ["only one prompt"], tools=[])

    def test_a_slot_answering_tool_call_text_under_no_tools_is_skipped_naming_tools(
        self,
    ):
        # Deux entrées, l'une répond en texte d'appel d'outil (DSML) sous un appel sans outils :
        # elle est marquée échouée avec `tools` en cause probable, l'autre est interrogée et rend
        # son avis — l'échec d'un slot n'entraîne pas celui du panel.
        dsml_call = (
            "<\uff5cDSML\uff5c tool_calls>\n"
            '<\uff5cDSML\uff5c invoke name="read">\n'
            "</\uff5cDSML\uff5c invoke>\n"
            "</\uff5cDSML\uff5c calls>"
        )
        with unittest.mock.patch.object(
            mp.ma,
            "call_cli",
            self._fake_cli(
                {"z-ai/glm-5-2": dsml_call, "mistral-medium-3.5": "a healthy review"}
            ),
        ):
            resp = mp.run_panel([GLM, MISTRAL], ["p1", "p2"], tools=[])
        self.assertEqual(
            resp.answers,
            [
                mp.Answer(
                    2,
                    "mistral-medium-3.5",
                    "a healthy review",
                    "mistral/mistral-medium-3.5 · 🟧 opus",
                )
            ],
        )
        self.assertEqual(resp.skipped[0].slot, 1)
        self.assertEqual(resp.skipped[0].model, "glm-5-2")
        self.assertEqual(resp.skipped[0].reason, mp.ma.TOOL_CALL_TEXT_ERROR)
        # Le rapport nomme `tools` comme cause probable de l'échec du slot.
        self.assertIn("tools", mp.format_panel(resp, requested=2))

    def test_each_slot_gets_its_own_prompt_and_the_requested_tools(self):
        seen: dict = {}
        with unittest.mock.patch.object(
            mp.ma,
            "call_cli",
            self._fake_cli({"z-ai/glm-5-2": "ok", "mistral-medium-3.5": "ok"}, seen),
        ):
            mp.run_panel(
                [GLM, MISTRAL], ["lens A prompt", "lens B prompt"], tools=["read"]
            )
        self.assertEqual(
            seen,
            {
                "z-ai/glm-5-2": ("lens A prompt", ["read"]),
                "mistral-medium-3.5": ("lens B prompt", ["read"]),
            },
        )


class TestRunPanelSecrets(unittest.TestCase):
    def test_keychain_read_once_and_given_to_every_slot(self):
        envs = []

        def fake_call_cli(entry, prompt, timeout, tools=None, env=None, events=None):
            envs.append(env)
            return "ok"

        with (
            unittest.mock.patch.object(
                mp.ma, "secret_env", return_value={"K": "v"}
            ) as secrets,
            unittest.mock.patch.object(mp.ma, "call_cli", fake_call_cli),
        ):
            mp.run_panel([GLM, MISTRAL], ["p1", "p2"], tools=[])
        self.assertEqual(secrets.call_count, 1)
        self.assertEqual(envs, [{"K": "v"}, {"K": "v"}])


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


class TestFormatPanelLabel(unittest.TestCase):
    def test_an_answer_with_a_label_is_tagged_with_it_instead_of_the_pool_name(self):
        resp = mp.PanelResponse(
            answers=[mp.Answer(1, "glm-5-2", "A", "openrouter/glm-5-2 · 🟧 opus")],
            skipped=[],
        )
        self.assertEqual(
            mp.format_panel(resp, requested=1),
            "[panel] 1/1 slots answered\n\n"
            "--- [slot 1 · openrouter/glm-5-2 · 🟧 opus] ---\nA",
        )


if __name__ == "__main__":
    unittest.main()


class TestRunPanelLiveView(unittest.TestCase):
    def test_each_slot_writes_its_own_view(self):
        def fake_call_cli(entry, prompt, timeout, tools=None, env=None, events=None):
            events.write(json.dumps({"type": "agent_settled", "aborted": False}) + "\n")
            return "ok" if entry is GLM else " "

        with tempfile.TemporaryDirectory() as cfg:
            with (
                unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}),
                unittest.mock.patch.object(mp.ma, "secret_env", return_value={}),
                unittest.mock.patch.object(mp.ma, "call_cli", fake_call_cli),
            ):
                run = mp.pi_live.open_run(str(mp.pi_live.runs_dir() / "toolu_p"))
                resp = mp.run_panel([GLM, MISTRAL], ["p1", "p2"], tools=[], live=run)
            views = {
                p.name: re.sub(r"\[ *\d+\.\ds\] ", "", p.read_text())
                for p in sorted(run.glob("view-*.txt"))
            }
        self.assertEqual(len(resp.answers), 1)
        self.assertEqual(
            views,
            {
                "view-1.txt": (
                    f"slot 1 · {GLM['name']}\nsettled\n"
                    f"-> {GLM['name']} attempt 1: answered\npool end\n"
                ),
                "view-2.txt": (
                    f"slot 2 · {MISTRAL['name']}\nsettled\n"
                    f"-> {MISTRAL['name']} attempt 1: empty answer\npool end\n"
                ),
            },
        )
