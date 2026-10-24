"""Tests for `model_sanitize` — the reasoning-out policy applied to every pool model's answer."""

from __future__ import annotations

import json
import os
import unittest
import unittest.mock
from typing import Any

import model_sanitize
from pi_fakes import FakePiProcess, pi_events

import model_adversarial as ma

DEEPSEEK = {
    "name": "sail-deepseek-v4-pro",
    "kind": "cli",
    "provider": "sail",
    "model": "deepseek-ai/DeepSeek-V4-Pro-0813",
    "capacity_tier": "opus",
    "free_tier": True,
    "concurrency": None,
}


class ExtractAnswerTest(unittest.TestCase):
    """`extract_answer` — separation at the source: text blocks only, never reasoning."""

    def test_text_blocks_only(self):
        message: dict[str, Any] = {
            "role": "assistant",
            "reasoning_content": "chain",
            "content": [
                {"type": "thinking", "thinking": "working notes"},
                {"type": "text", "text": "A"},
                {"type": "tool_call", "name": "read"},
                {"type": "text", "text": "B"},
            ],
            "stopReason": "stop",
        }
        self.assertEqual(model_sanitize.extract_answer(message), "AB")


class SanitizeAnswerTest(unittest.TestCase):
    """`sanitize_answer` — inline reasoning stripped, clean text byte-identical."""

    def test_clean_report_passthrough_byte_identical(self):
        report = (
            "## Findings\n"
            "\n"
            "- the guard misses case X\n"
            "- costs, per table:\n"
            "\n"
            "| a | b |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "\n"
            "## Verdict\n"
            "\n"
            "One real bug, one simplification.\n"
        )
        self.assertEqual(model_sanitize.sanitize_answer(report), report)

    def test_paired_think_block_removed(self):
        text = "Intro.\n<think>\ninternal notes\n</think>\n\n## Findings\n- one"
        self.assertEqual(
            model_sanitize.sanitize_answer(text),
            "Intro.\n\n\n## Findings\n- one",
        )

    def test_paired_rf_thinking_block_removed(self):
        text = "|RF-thinking|\nhidden deliberation\n||\n\n## Verdict\nfine"
        self.assertEqual(
            model_sanitize.sanitize_answer(text),
            "\n\n## Verdict\nfine",
        )

    def test_stray_close_keeps_tail_after_last(self):
        # Forme mesurée : l'opener est mangé par le fournisseur, seul le closer survit —
        # parfois plusieurs fois, le raisonnement en cite un lui-même.
        text = (
            "Let me think.\n## Working\nnotes part one\n</think>\n"
            "more notes\n</think>\n## Findings\nthe report"
        )
        self.assertEqual(
            model_sanitize.sanitize_answer(text),
            "## Findings\nthe report",
        )

    def test_unterminated_opener_drops_rest(self):
        self.assertEqual(
            model_sanitize.sanitize_answer("Valid.\n<think>\nrambles forever"),
            "Valid.\n",
        )
        self.assertEqual(model_sanitize.sanitize_answer("<think>\nonly reasoning"), "")

    def test_lone_rf_opener_token_removed(self):
        self.assertEqual(
            model_sanitize.sanitize_answer("Answer here |RF-thinking| and more"),
            "Answer here  and more",
        )

    def test_leading_deliberation_before_heading_dropped(self):
        text = "Now I have enough to form findings. Let me verify each one.\n\n## Findings\n- one"
        self.assertEqual(model_sanitize.sanitize_answer(text), "## Findings\n- one")

    def test_intro_without_deliberation_kept(self):
        text = "Short intro.\n\n## Findings\n- one"
        self.assertEqual(model_sanitize.sanitize_answer(text), text)

    def test_deliberation_without_structural_marker_kept(self):
        # Au doute on garde : sans marqueur structurel, rien ne distingue la délibération
        # d'une réponse en prose — tout est conservé.
        text = "I will answer directly: the config flag is inverted."
        self.assertEqual(model_sanitize.sanitize_answer(text), text)


class IncludeReasoningTest(unittest.TestCase):
    """`include_reasoning` — the `PI_INCLUDE_REASONING` escape hatch."""

    def test_off_by_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PI_INCLUDE_REASONING", None)
            self.assertFalse(model_sanitize.include_reasoning())

    def test_on_when_1(self):
        with unittest.mock.patch.dict(os.environ, {"PI_INCLUDE_REASONING": "1"}):
            self.assertTrue(model_sanitize.include_reasoning())


class CallCliSanitizedTest(unittest.TestCase):
    """`call_cli` end to end on a faked pi process — the payload a tool result carries."""

    RAW_TEXT = "<think>\nnotes\n</think>\n\n## Findings\n- one"

    def _run(self) -> str:
        events = pi_events(
            text=self.RAW_TEXT, reasoning_content="chain", thinking="block notes"
        )
        proc = FakePiProcess(events, "", 0, None, [])
        with unittest.mock.patch("model_adversarial.Popen", return_value=proc):
            return ma.call_cli(DEEPSEEK, "p", 30, [])

    def test_payload_has_no_reasoning_and_raw_is_kept(self):
        answer = self._run()
        self.assertEqual(answer, "\n\n## Findings\n- one")
        for marker in ("<think>", "</think>", "reasoning_content"):
            self.assertNotIn(marker, answer)
        raw = model_sanitize.raw_log_path()
        self.assertTrue(raw.exists(), raw)
        record = json.loads(raw.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["entry"], DEEPSEEK["name"])
        self.assertEqual(record["message"]["reasoning_content"], "chain")
        self.assertEqual(record["message"]["content"][0]["thinking"], "block notes")

    def test_env_flag_restores_verbatim(self):
        with unittest.mock.patch.dict(os.environ, {"PI_INCLUDE_REASONING": "1"}):
            self.assertEqual(self._run(), self.RAW_TEXT)

    def test_clean_answer_unchanged_through_call_cli(self):
        events = pi_events(text="## Findings\n- one\n")
        proc = FakePiProcess(events, "", 0, None, [])
        with unittest.mock.patch("model_adversarial.Popen", return_value=proc):
            self.assertEqual(ma.call_cli(DEEPSEEK, "p", 30, []), "## Findings\n- one\n")


class RunPoolSanitizedTest(unittest.TestCase):
    """`run_pool` returns the sanitized answer — the payload every tool result builds on."""

    def test_pool_result_is_sanitized(self):
        events = pi_events(
            text="Let me look again.\n</think>\n## Findings\nthe report",
            reasoning_content="chain",
        )
        proc = FakePiProcess(events, "", 0, None, [])
        with unittest.mock.patch("model_adversarial.Popen", return_value=proc):
            result = ma.run_pool([DEEPSEEK], "p", tools=[])
        self.assertEqual(result, ma.PoolResult(DEEPSEEK, "## Findings\nthe report"))


if __name__ == "__main__":
    unittest.main()
