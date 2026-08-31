#!/usr/bin/env python3
"""Tests for plugins/pc-core/mcp/review_complexity.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import review_complexity as rc


def _diff(path: str, added: int, removed: int = 0) -> str:
    """A minimal unified-diff block for one file with `added`/`removed` content lines."""
    head = [
        f"diff --git a/{path} b/{path}",
        "index 0000000..1111111 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,{removed} +1,{added} @@",
    ]
    body = [f"+added line {i}" for i in range(added)] + [
        f"-removed line {i}" for i in range(removed)
    ]
    return "\n".join(head + body) + "\n"


class TestAnalyze(unittest.TestCase):
    def test_counts_files_and_changed_lines(self):
        stats = rc.analyze(_diff("src/a.py", 10) + _diff("src/b.py", 5, 3))
        self.assertEqual(stats.files, 2)
        self.assertEqual(stats.changed_lines, 18)

    def test_test_file_lines_are_counted_separately(self):
        stats = rc.analyze(_diff("tests/test_a.py", 12))
        self.assertEqual(stats.test_lines, 12)
        self.assertNotIn("no-test-changes", stats.risk_factors)

    def test_no_test_changes_risk_over_threshold(self):
        stats = rc.analyze(_diff("src/a.py", 80))
        self.assertIn("no-test-changes", stats.risk_factors)

    def test_security_sensitive_path_is_flagged(self):
        stats = rc.analyze(_diff("src/api/permissions.py", 4))
        self.assertTrue(
            any(r.startswith("security-sensitive:") for r in stats.risk_factors)
        )

    def test_sql_migration_is_flagged(self):
        stats = rc.analyze(_diff("resources/migrations/001_init.sql", 20))
        self.assertIn("db-migration", stats.risk_factors)

    def test_empty_diff_is_zeroed_not_raised(self):
        stats = rc.analyze("")
        self.assertEqual((stats.files, stats.changed_lines), (0, 0))


class TestTierFor(unittest.TestCase):
    def test_small_mechanical_diff_is_haiku(self):
        self.assertEqual(rc.tier_for(_diff("src/util.py", 12)), "haiku")

    def test_moderate_diff_is_sonnet(self):
        self.assertEqual(rc.tier_for(_diff("src/service.py", 120)), "sonnet")

    def test_many_files_is_sonnet_even_when_small(self):
        many = "".join(_diff(f"src/m{i}.py", 3) for i in range(7))
        self.assertEqual(rc.tier_for(many), "sonnet")

    def test_large_diff_is_opus(self):
        self.assertEqual(rc.tier_for(_diff("src/huge.py", 500)), "opus")

    def test_security_sensitive_diff_is_opus_however_small(self):
        self.assertEqual(rc.tier_for(_diff("src/auth.py", 3)), "opus")

    def test_two_diffs_two_tiers(self):
        # FR-0023-26 — the tier is a function of the diff, not a human pick
        small = rc.tier_for(_diff("src/util.py", 10))
        big = rc.tier_for(_diff("src/core.py", 600))
        self.assertNotEqual(small, big)


if __name__ == "__main__":
    unittest.main()
