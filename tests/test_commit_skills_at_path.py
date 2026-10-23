"""The `commit` and `commit-and-pr` skills are model-run (`context: fork`): they receive a typed
`@<path>` argument as literal text, and the direct-command runner does not apply to them. Their
body must tell the model that a word starting with `@` is a path."""

import unittest
from pathlib import Path

SKILLS = Path(__file__).parent.parent / "plugins" / "pc-git" / "skills"


class AtPrefixedPathCase(unittest.TestCase):
    def test_each_skill_reads_an_at_prefixed_word_as_a_path(self):
        for name in ("commit", "commit-and-pr"):
            with self.subTest(skill=name):
                flat = " ".join((SKILLS / name / "SKILL.md").read_text().split())
                self.assertIn(
                    "**A word starting with `@` is a path**: drop the `@`", flat
                )
                self.assertIn("resolve it against `${CLAUDE_PROJECT_DIR}`", flat)


if __name__ == "__main__":
    unittest.main()
