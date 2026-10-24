"""Tests for the reconcile oracle SCORER logic — no network (the live scoring is
plugins/pc-pypi/skills/dev-prerelease/evals/reconcile_oracle.py).

Pins the two failures the oracle exists to catch: a rubber-stamp (recall miss) and a known false
positive (precision violation). If the scorer stopped detecting either, a broken framing would score
green — the whole point lost.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-pypi"
        / "skills"
        / "dev-prerelease"
        / "evals"
    ),
)

import reconcile_oracle as ro


def _case(must_flag=(), must_not_flag=()):
    return ro.Case(
        name="t",
        table_md="",
        sources={},
        must_flag=list(must_flag),
        must_not_flag=list(must_not_flag),
    )


class TestScorer(unittest.TestCase):
    def test_rubber_stamp_is_a_recall_fail(self):
        answer = "VERDICT: 0 diverge, 0 missing"
        s = ro.score_case(
            _case(must_flag=["api_integration", "compute_pool", "grant"]), answer
        )
        self.assertEqual(s.recall_misses, ["api_integration", "compute_pool", "grant"])
        self.assertFalse(s.passed)

    def test_all_broken_promises_named_passes_recall(self):
        answer = (
            "DIVERGE: api_integration is a stub\n"
            "DIVERGE: compute_pool is a stub\n"
            "DIVERGE: grant is a stub\n"
            "VERDICT: 3 diverge, 0 missing"
        )
        s = ro.score_case(
            _case(must_flag=["api_integration", "compute_pool", "grant"]), answer
        )
        self.assertEqual(s.recall_misses, [])
        self.assertTrue(s.passed)

    def test_known_false_positive_is_a_precision_fail(self):
        # the v2 issue: it demands adding purge_expired, which already exists in the table
        answer = (
            "MISSING: `purge_expired` should be added\nVERDICT: 0 diverge, 1 missing"
        )
        s = ro.score_case(_case(must_not_flag=["purge_expired"]), answer)
        self.assertEqual(s.precision_violations, ["purge_expired"])
        self.assertFalse(s.passed)

    def test_clean_answer_passes_precision(self):
        answer = "Rien à signaler.\nVERDICT: 0 diverge, 0 missing"
        s = ro.score_case(_case(must_not_flag=["purge_expired"]), answer)
        self.assertEqual(s.precision_violations, [])
        self.assertTrue(s.passed)

    def test_object_name_inside_a_compound_word_is_not_a_flag(self):
        answer = (
            "DIVERGE: `grant` is a stub\n"
            "Ligne corrigée: `| grant | ⚠ | database-scoped privilege grant |`\n"
            "VERDICT: 1 diverge, 0 missing"
        )
        s = ro.score_case(
            _case(must_flag=["grant"], must_not_flag=["database"]), answer
        )
        self.assertEqual(s.recall_misses, [])
        self.assertEqual(s.precision_violations, [])
        self.assertTrue(s.passed)

    def test_verdict_line_is_not_scored_as_body(self):
        answer = "Rien.\nVERDICT: purge_expired 0 diverge, 0 missing"
        s = ro.score_case(_case(must_not_flag=["purge_expired"]), answer)
        self.assertEqual(s.precision_violations, [])


class TestBriefBuilders(unittest.TestCase):
    def test_oracle_guards_every_brief_path(self):
        # the oracle scores the shipped brief (signature) plus the two baselines
        self.assertEqual(set(ro.BRIEF_BUILDERS), {"signature", "all", "selective"})
        rc = {c.name: c for c in ro.load_cases()}["recall_broken_promises"]
        sig = ro.BRIEF_BUILDERS["signature"](rc)
        self.assertIn("⚠ STUB", sig)  # the deterministic flag the small model leans on
        self.assertIn("ApiIntegration", sig)
        selective = ro.BRIEF_BUILDERS["selective"](rc)
        self.assertIn("PACKAGE INDEX", selective)

    def test_run_case_uses_the_requested_builder(self):
        rc = {c.name: c for c in ro.load_cases()}["recall_broken_promises"]
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return "VERDICT: 0 diverge, 0 missing"

        ro.run_case(rc, fake, "selective")
        self.assertIn("PACKAGE INDEX", seen["prompt"])
        ro.run_case(rc, fake, "all")
        self.assertNotIn("PACKAGE INDEX", seen["prompt"])

    def test_run_case_prepends_the_phase1_prompt(self):
        rc = {c.name: c for c in ro.load_cases()}["precision_clean_table"]
        seen = {}

        def fake(prompt):
            seen["prompt"] = prompt
            return "VERDICT: 0 diverge, 0 missing"

        ro.run_case(rc, fake, "all")
        self.assertTrue(seen["prompt"].startswith(ro.rc.PHASE1_TASK))


class TestFixtures(unittest.TestCase):
    def test_fixtures_load_and_are_well_formed(self):
        cases = {c.name: c for c in ro.load_cases()}
        self.assertIn("recall_broken_promises", cases)
        self.assertIn("precision_clean_table", cases)
        recall = cases["recall_broken_promises"]
        self.assertEqual(
            set(recall.must_flag), {"api_integration", "compute_pool", "grant"}
        )
        self.assertTrue(recall.sources, "recall case must ship source files")
        # the complete reference must be in the brief so the model has the contrast
        self.assertTrue(any("database.py" in p for p in recall.sources))
        prec = cases["precision_clean_table"]
        self.assertEqual(prec.must_not_flag, ["purge_expired"])
        self.assertTrue(any("ai.py" in p for p in prec.sources))


if __name__ == "__main__":
    unittest.main()
