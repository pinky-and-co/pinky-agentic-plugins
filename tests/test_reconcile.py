#!/usr/bin/env python3
"""Tests for plugins/pc-pypi/skills/dev-prerelease/scripts/reconcile.py — run with:
python3 -m unittest discover tests
"""

import contextlib
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-pypi"
        / "skills"
        / "dev-prerelease"
        / "scripts"
    ),
)

import reconcile as rc

_SRC = "class Quota:\n    async def reset(self):\n        pass\n\ndef get_ai():\n    pass\n"

_TABLE = (
    "| Feature | Module | Status |\n"
    "|---|---|---|\n"
    "| XML parsing via `parse_xml` and `xml_path_str` | `xml` | stable |\n"
    "| Quota tracking | `quota` | stable |\n"
)

_SOURCES = {
    "src/lib/ai.py": "class Ai:\n    def get(self): pass\n",
    "src/lib/util.py": "PLAIN = 1\n",
}


class TestParseFeatureRows(unittest.TestCase):
    def test_reads_a_matching_three_column_table(self):
        md = (
            "| Feature | Module | Status |\n"
            "|---|---|---|\n"
            "| `get_ai` embeds text | `ai` | stable |\n"
            "| `purge` old rows | `ai` | planned |\n"
        )
        self.assertEqual(
            rc.parse_feature_rows(md),
            [
                rc.Row("`get_ai` embeds text", "ai", "stable"),
                rc.Row("`purge` old rows", "ai", "planned"),
            ],
        )

    def test_abstains_on_an_unrecognised_three_column_header(self):
        md = "| Object | MVP | Notes |\n|---|---|---|\n| `quota` | yes | per user |\n"
        self.assertEqual(rc.parse_feature_rows(md), [])

    def test_header_is_a_parameter(self):
        md = (
            "| Object | Module | Notes |\n"
            "|---|---|---|\n"
            "| a thing | `core` | shipped |\n"
        )
        self.assertEqual(
            rc.parse_feature_rows(md, header=("object", "module", "notes")),
            [rc.Row("a thing", "core", "shipped")],
        )

    def test_status_normalises_to_stable_planned_or_verbatim_lower(self):
        md = (
            "| Feature | Module | Status |\n"
            "|---|---|---|\n"
            "| a | `m` | Stable ✓ |\n"
            "| b | `m` | Planned (Q3) |\n"
            "| c | `m` | Deprecated |\n"
        )
        self.assertEqual(
            [r.status for r in rc.parse_feature_rows(md)],
            ["stable", "planned", "deprecated"],
        )

    def test_a_non_table_line_closes_the_table(self):
        md = (
            "| Feature | Module | Status |\n"
            "|---|---|---|\n"
            "| a | `m` | stable |\n"
            "\n"
            "| a | `m` | stable |\n"  # second table, no header -> not read
        )
        self.assertEqual(len(rc.parse_feature_rows(md)), 1)

    def test_column_roles_resolved_by_name_not_position(self):
        # Object|Status|Notes: no free-text feature column, module is col 0, ✓ -> stable
        md = (
            "| Object | Status | Notes |\n"
            "|---|---|---|\n"
            "| grant | ✓ | database-scoped privilege grant |\n"
        )
        self.assertEqual(
            rc.parse_feature_rows(md, header=("object", "status", "notes")),
            [rc.Row("database-scoped privilege grant", "grant", "stable")],
        )


class TestClassifySymbol(unittest.TestCase):
    def test_sql_all_caps_is_skipped(self):
        self.assertIsNone(rc.classify_symbol("AI_EMBED"))
        self.assertIsNone(rc.classify_symbol("SNOWFLAKE.CORTEX.COUNT_TOKENS"))

    def test_capword_is_a_class(self):
        self.assertEqual(rc.classify_symbol("PerUserQuota"), ("class", "PerUserQuota"))

    def test_snake_is_a_function(self):
        self.assertEqual(rc.classify_symbol("purge_expired"), ("def", "purge_expired"))

    def test_class_dot_method_checks_the_method(self):
        self.assertEqual(rc.classify_symbol("Quota.reset"), ("def", "reset"))


class TestSymbolPresent(unittest.TestCase):
    def test_finds_class_and_async_def_and_plain_def(self):
        self.assertTrue(rc.symbol_present(_SRC, "class", "Quota"))
        self.assertTrue(rc.symbol_present(_SRC, "def", "reset"))
        self.assertTrue(rc.symbol_present(_SRC, "def", "get_ai"))

    def test_absent_symbol_is_false(self):
        self.assertFalse(rc.symbol_present(_SRC, "def", "missing"))


class TestCheckRow(unittest.TestCase):
    def test_stable_but_absent_is_a_finding(self):
        findings, residue = rc.check_row(
            rc.Row("`get_ai` does x", "ai", "stable"), "def other(): pass"
        )
        self.assertEqual([f.kind for f in findings], ["stable-absent"])
        self.assertFalse(residue)

    def test_planned_but_present_is_a_finding(self):
        findings, _ = rc.check_row(
            rc.Row("`get_ai` does x", "ai", "planned"), "def get_ai(): pass"
        )
        self.assertEqual([f.kind for f in findings], ["planned-present"])

    def test_missing_module_is_a_finding(self):
        findings, residue = rc.check_row(rc.Row("x", "ghost", "stable"), None)
        self.assertEqual([f.kind for f in findings], ["module-missing"])
        self.assertFalse(residue)

    def test_stable_and_present_is_residue_for_the_model(self):
        findings, residue = rc.check_row(
            rc.Row("`get_ai` does x", "ai", "stable"), "def get_ai(): pass"
        )
        self.assertEqual(findings, [])
        self.assertTrue(residue)


class TestScan(unittest.TestCase):
    def _repo(self, table: str, modules: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "docs").mkdir()
        (root / "docs" / "features.md").write_text(table, encoding="utf-8")
        pkg = root / "src" / "lib"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        for name, src in modules.items():
            (pkg / f"{name}.py").write_text(src, encoding="utf-8")
        return root

    def test_end_to_end_finding_and_residue(self):
        table = (
            "| Feature | Module | Status |\n"
            "|---|---|---|\n"
            "| `get_ai` embeds | `ai` | stable |\n"
            "| `not_built` yet | `ai` | stable |\n"
        )
        repo = self._repo(table, {"ai": "def get_ai(): pass\n"})
        result = rc.scan(repo, "docs/features.md")
        self.assertEqual([f.symbol for f in result.findings], ["not_built"])
        self.assertEqual([r.feature for r in result.residue], ["`get_ai` embeds"])


class TestNotApplicable(unittest.TestCase):
    def _repo(self, doc_text: str | None, with_package: bool = True) -> Path:
        root = Path(tempfile.mkdtemp())
        if doc_text is not None:
            (root / "docs").mkdir()
            (root / "docs" / "f.md").write_text(doc_text, encoding="utf-8")
        if with_package:
            pkg = root / "src" / "lib"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
        return root

    def test_missing_doc_is_not_applicable(self):
        repo = self._repo(None)
        self.assertIsNotNone(rc.not_applicable(repo, "docs/f.md"))

    def test_doc_without_a_recognised_table_is_not_applicable_not_clean(self):
        repo = self._repo("# Features\n\nProse only, no table.\n")
        reason = rc.not_applicable(repo, "docs/f.md")
        self.assertIsNotNone(reason)
        self.assertIn("no recognised", reason)

    def test_no_single_package_is_not_applicable(self):
        repo = self._repo(
            "| Feature | Module | Status |\n|---|---|---|\n| a | `m` | stable |\n",
            with_package=False,
        )
        self.assertEqual(
            rc.not_applicable(repo, "docs/f.md"),
            "no single importable package under src/",
        )

    def test_a_readable_repo_is_applicable(self):
        repo = self._repo(
            "| Feature | Module | Status |\n|---|---|---|\n| a | `m` | stable |\n"
        )
        self.assertIsNone(rc.not_applicable(repo, "docs/f.md"))


class TestDropAlreadyDocumented(unittest.TestCase):
    def test_fr_0026_06_capability_named_inside_another_row_is_dropped(self):
        # the model wrongly demands a dedicated row for `xml_path_str` — it is already named in the
        # `xml` row's Feature cell
        self.assertEqual(
            rc.drop_already_documented(["helpers: xml_path_str"], _TABLE),
            [],
        )

    def test_capability_whose_module_already_has_a_row_is_dropped(self):
        self.assertEqual(
            rc.drop_already_documented(["quota: PerUserQuota"], _TABLE),
            [],
        )

    def test_a_real_omission_survives_the_filter(self):
        self.assertEqual(
            rc.drop_already_documented(["billing: charge_card"], _TABLE),
            ["billing: charge_card"],
        )


class TestStubDetection(unittest.TestCase):
    def _cls(self, body: str):
        import ast

        return ast.parse(body).body[0]

    def test_docstring_only_class_is_a_stub(self):
        self.assertTrue(rc._is_stub_body(self._cls('class C:\n    "doc"\n')))

    def test_pass_and_ellipsis_and_notimplemented_are_stubs(self):
        self.assertTrue(rc._is_stub_body(self._cls("def f():\n    pass\n")))
        self.assertTrue(rc._is_stub_body(self._cls("def f():\n    ...\n")))
        self.assertTrue(
            rc._is_stub_body(self._cls("def f():\n    raise NotImplementedError\n"))
        )

    def test_a_real_body_is_not_a_stub(self):
        self.assertFalse(rc._is_stub_body(self._cls("def f():\n    return 1\n")))
        self.assertFalse(rc._is_stub_body(self._cls("class C:\n    x: int = 1\n")))


class TestNormalizeManquants(unittest.TestCase):
    def test_bare_capability_line(self):
        out = rc.normalize_missing_findings(
            "MISSING — `xml_path_str` : fonction publique absente du tableau\n"
            "VERDICT: 0 diverge, 1 missing\n"
        )
        self.assertEqual(out, ["xml_path_str"])

    def test_module_prefix_when_the_line_names_it(self):
        out = rc.normalize_missing_findings(
            "MISSING in `xml`: `xml_path_str` has no row\n"
        )
        self.assertEqual(out, ["xml: xml_path_str"])

    def test_a_word_ending_in_in_is_not_the_module_marker(self):
        out = rc.normalize_missing_findings("MISSING — the domain `iam` lacks a row\n")
        self.assertEqual(out, ["iam"])

    def test_call_parens_are_stripped_and_verdict_line_ignored(self):
        out = rc.normalize_missing_findings(
            "MISSING: `charge_card()` — méthode publique de billing\n"
            "VERDICT: 0 diverge, 1 MISSING\n"
        )
        self.assertEqual(out, ["charge_card"])

    def test_missing_line_without_a_symbol_is_dropped(self):
        self.assertEqual(
            rc.normalize_missing_findings("Aucun MISSING à signaler.\n"), []
        )

    def test_no_missing_returns_empty(self):
        self.assertEqual(
            rc.normalize_missing_findings(
                "DIVERGE: `get_ai` est un stub\nVERDICT: 1 diverge, 0 missing"
            ),
            [],
        )

    def test_output_feeds_drop_already_documented(self):
        # the whole point: the normalised list is exactly drop_already_documented's input contract
        raw = "MISSING — `xml_path_str` doit être ajouté\nMISSING — `charge_card` de billing\n"
        self.assertEqual(
            rc.drop_already_documented(rc.normalize_missing_findings(raw), _TABLE),
            ["charge_card"],  # xml_path_str is named inside the xml row -> dropped
        )


class TestGeneratorsHaveAnOracle(unittest.TestCase):
    def test_build_structural_index_full_output(self):
        self.assertEqual(
            rc.build_structural_index(_SOURCES),
            "src/lib/ai.py: Ai, get\nsrc/lib/util.py",
        )

    def test_build_selective_brief_full_output(self):
        doc = "The `Ai` class in ai does the work."
        self.assertEqual(
            rc.build_selective_brief("docs/f.md", doc, _SOURCES),
            "--- DOCUMENT TO RECONCILE (docs/f.md) ---\n"
            "The `Ai` class in ai does the work.\n\n"
            "--- PACKAGE INDEX (all files, public symbols — to spot code that is implemented "
            "but ABSENT from the document) ---\n"
            "src/lib/ai.py: Ai, get\nsrc/lib/util.py\n\n"
            "--- CODE OF THE MODULES THE DOCUMENT CITES (full source — to judge whether the code "
            "does what the document claims) ---\n\n"
            "--- CODE: src/lib/ai.py ---\n```python\nclass Ai:\n    def get(self): pass\n\n```",
        )

    def test_build_signature_brief_full_output(self):
        self.assertEqual(
            rc.build_signature_brief("docs/f.md", "The doc.", _SOURCES),
            "--- FEATURE TABLE (docs/f.md) ---\n"
            "The doc.\n\n"
            "--- PUBLIC API (signatures + first docstring line; ⚠ STUB = empty/trivial body, "
            "to be treated as NOT IMPLEMENTED whatever the docstring or the table claims) ---\n\n"
            "### src/lib/ai.py\n"
            "class Ai\n"
            "    def get(self)  ⚠ STUB",
        )

    def test_build_phase1_brief_full_output(self):
        self.assertEqual(
            rc.build_phase1_brief("docs/f.md", "The doc.", _SOURCES),
            "--- FEATURE TABLE (docs/f.md) ---\n"
            "The doc.\n\n"
            "--- CODE: src/lib/ai.py ---\n```python\nclass Ai:\n    def get(self): pass\n\n```\n\n"
            "--- CODE: src/lib/util.py ---\n```python\nPLAIN = 1\n\n```",
        )

    def test_render_full_output(self):
        result = rc.ScanResult(
            findings=[
                rc.Finding(
                    "stable-absent", "ai", "x", "`x` marked stable but absent from `ai`"
                )
            ],
            residue=[rc.Row("`y` works", "ai", "stable")],
        )
        self.assertEqual(
            rc.render(Path("/tmp/mylib"), result),
            "# Reconcile (deterministic) — mylib\n"
            "\n"
            "## Deterministic divergences (1) — fix these\n"
            "- **stable-absent** · `x` marked stable but absent from `ai`\n"
            "\n"
            "## Model residue (1 rows) — present, semantic correctness unverified\n"
            "- `ai` · `y` works",
        )


class TestCliModes(unittest.TestCase):
    def _repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "docs").mkdir()
        (root / "docs" / "f.md").write_text(
            "| Feature | Module | Status |\n"
            "|---|---|---|\n"
            "| `get_ai` factory | `ai` | stable |\n",
            encoding="utf-8",
        )
        pkg = root / "src" / "lib"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "ai.py").write_text(
            "def get_ai():\n    return 1\n\ndef _hidden():\n    pass\n",
            encoding="utf-8",
        )
        return root

    def test_emit_prompt_outputs_phase1_task_then_signature_brief(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rc.main(
                [
                    "--repo",
                    str(self._repo()),
                    "--features-doc",
                    "docs/f.md",
                    "--emit-prompt",
                ]
            )
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(rc.PHASE1_TASK))
        self.assertIn("--- PUBLIC API", out)
        self.assertIn("def get_ai()", out)
        self.assertNotIn("_hidden", out)  # private symbols stay out of the brief

    def test_filter_model_output_reads_stdin_and_drops_documented(self):
        repo = str(self._repo())
        stdin = io.StringIO(
            "MISSING: `charge_card` (billing)\nMISSING: `get_ai` needs a row\n"
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), unittest.mock.patch("sys.stdin", stdin):
            code = rc.main(
                [
                    "--repo",
                    repo,
                    "--features-doc",
                    "docs/f.md",
                    "--filter-model-output",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().splitlines(), ["charge_card"])


if __name__ == "__main__":
    unittest.main()
