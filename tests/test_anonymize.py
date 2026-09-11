#!/usr/bin/env python3
"""Tests for plugins/pc-privacy/mcp/anonymize.py.

The deterministic core (pseudonym coherence, salt stability, report formatting, IO errors) is
covered without Presidio. One test is gated on the spaCy model being installed.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "mcp"))

import anonymize


def _csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


# ── pseudonym coherence — the whole point ────────────────────────────────────


def test_same_value_gets_same_pseudonym_across_files(tmp_path):
    """A join key shared by two files must map to one pseudonym on both sides."""
    orders = _csv(
        tmp_path / "orders.csv",
        [
            {"customer": "Jean Dupont", "amount": "10"},
            {"customer": "Marie Sy", "amount": "20"},
        ],
    )
    tickets = _csv(
        tmp_path / "tickets.csv",
        [{"customer": "Jean Dupont", "issue": "late"}],
    )

    outcome = anonymize.apply_anonymization(
        [str(orders), str(tickets)], ["customer"], salt="fixed"
    )

    o_rows = list(csv.DictReader((tmp_path / "orders.anon.csv").open(encoding="utf-8")))
    t_rows = list(
        csv.DictReader((tmp_path / "tickets.anon.csv").open(encoding="utf-8"))
    )
    assert o_rows[0]["customer"] == t_rows[0]["customer"]  # join preserved
    assert (
        o_rows[0]["customer"] != o_rows[1]["customer"]
    )  # distinct people, distinct pseudonym
    assert "Jean Dupont" not in (tmp_path / "orders.anon.csv").read_text(
        encoding="utf-8"
    )
    assert outcome.unique_values == 2


def test_salt_is_stable_across_runs_and_changes_the_output(tmp_path):
    data = [{"name": "Alice"}]
    a = anonymize.apply_anonymization(
        [str(_csv(tmp_path / "a.csv", data))], ["name"], salt="s1"
    )
    anonymize.apply_anonymization(
        [str(_csv(tmp_path / "b.csv", data))], ["name"], salt="s1"
    )
    anonymize.apply_anonymization(
        [str(_csv(tmp_path / "c.csv", data))], ["name"], salt="s2"
    )

    pa = (tmp_path / "a.anon.csv").read_text(encoding="utf-8")
    pb = (tmp_path / "b.anon.csv").read_text(encoding="utf-8")
    pc = (tmp_path / "c.anon.csv").read_text(encoding="utf-8")
    assert pa == pb  # same salt → identical pseudonym
    assert pa != pc  # different salt → different pseudonym
    assert a.salt == "s1"


def test_generated_salt_is_returned_when_not_pinned(tmp_path):
    outcome = anonymize.apply_anonymization(
        [str(_csv(tmp_path / "x.csv", [{"name": "Bob"}]))], ["name"]
    )
    assert outcome.salt and len(outcome.salt) >= 8
    # reusing the returned salt reproduces the pseudonyms
    again = anonymize.apply_anonymization(
        [str(_csv(tmp_path / "y.csv", [{"name": "Bob"}]))], ["name"], salt=outcome.salt
    )
    assert (tmp_path / "x.anon.csv").read_text(encoding="utf-8") == (
        tmp_path / "y.anon.csv"
    ).read_text(encoding="utf-8")
    assert again.salt == outcome.salt


def test_apply_skips_a_file_with_no_matching_column(tmp_path):
    orders = _csv(tmp_path / "orders.csv", [{"customer": "Jean", "amount": "10"}])
    other = _csv(tmp_path / "other.csv", [{"widget": "x"}])

    outcome = anonymize.apply_anonymization(
        [str(orders), str(other)], ["customer"], salt="s"
    )

    assert "other.csv" in outcome.skipped
    assert "orders.csv" not in outcome.skipped
    assert (tmp_path / "other.anon.csv").read_text(encoding="utf-8") == other.read_text(
        encoding="utf-8"
    )


# ── XML ─────────────────────────────────────────────────────────────────────


def test_xml_masks_leaf_tags_by_local_name_and_skips_containers(tmp_path):
    xml = tmp_path / "peci.xml"
    xml.write_text(
        "<peci:Employee><peci:First_Name>Jean</peci:First_Name>"
        "<peci:Legal_Name><peci:Last_Name>Dupont</peci:Last_Name></peci:Legal_Name></peci:Employee>",
        encoding="utf-8",
    )

    anonymize.apply_anonymization([str(xml)], ["First_Name", "Last_Name"], salt="s")

    out = (tmp_path / "peci.anon.xml").read_text(encoding="utf-8")
    assert "Jean" not in out
    assert "Dupont" not in out
    assert "FIRST_NAME_" in out
    assert "<peci:Legal_Name>" in out  # container tag untouched


def test_detect_report_never_contains_a_sampled_value():
    findings = [
        anonymize.Finding("clients.csv", "email", "detected EMAIL_ADDRESS (12/15)"),
        anonymize.Finding(
            "clients.csv", "nom", "column/tag name matches a PII keyword"
        ),
    ]
    report = anonymize.format_detect_report(findings)
    assert "email" in report and "clients.csv" in report
    for leaked in ("jean.dupont@example.fr", "Jean Dupont", "0612345678"):
        assert leaked not in report


def test_apply_report_carries_the_salt_and_no_values(tmp_path):
    outcome = anonymize.apply_anonymization(
        [str(_csv(tmp_path / "d.csv", [{"name": "Jean Dupont"}]))],
        ["name"],
        salt="abc123",
    )
    report = anonymize.format_apply_report(outcome)
    assert "salt: abc123" in report
    assert "Jean Dupont" not in report
    assert "d.anon.csv" in report


def test_empty_findings_report_tells_the_user_to_verify_manually():
    report = anonymize.format_detect_report([])
    assert "verify manually" in report


# ── IO errors raise instead of exiting the process ──────────────────────────


def test_unsupported_extension_raises_anonymize_error(tmp_path):
    bad = tmp_path / "data.txt"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(anonymize.AnonymizeError):
        anonymize._read_rows(bad)


def test_non_array_json_raises_anonymize_error(tmp_path):
    bad = tmp_path / "data.json"
    bad.write_text(json.dumps({"not": "an array"}), encoding="utf-8")
    with pytest.raises(anonymize.AnonymizeError):
        anonymize._read_rows(bad)


def test_write_rows_guards_an_unsupported_extension(tmp_path):
    with pytest.raises(anonymize.AnonymizeError):
        anonymize._write_rows(tmp_path / "x.txt", ["a"], [{"a": "1"}])


# ── the MCP tools themselves (needs `mcp`) ──────────────────────────────────


def test_server_apply_tool_runs_offline(tmp_path):
    """`anonymize_apply` is pure stdlib — it must work with no model, no network (FR-0023-20)."""
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "mcp")
    )
    import server

    csv_path = _csv(
        tmp_path / "orders.csv", [{"customer": "Jean Dupont", "amount": "9"}]
    )
    out = server.anonymize_apply([str(csv_path)], ["customer"], salt="s")
    assert "salt: s" in out
    assert "Jean Dupont" not in out
    assert "Jean Dupont" not in (tmp_path / "orders.anon.csv").read_text(
        encoding="utf-8"
    )


def test_server_detect_tool_returns_blocked_without_presidio(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    sys.path.insert(
        0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "mcp")
    )
    import server

    monkeypatch.setattr(server.anonymize, "preflight", lambda: "model missing")
    out = server.anonymize_detect([str(_csv(tmp_path / "c.csv", [{"name": "x"}]))])
    assert out.startswith("[anonymize_detect] BLOCKED")


def test_detect_raises_when_presidio_or_model_missing_via_preflight(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(anonymize, "preflight", lambda: "spaCy model not installed")
    with pytest.raises(anonymize.AnonymizeError, match="spaCy model"):
        anonymize.detect_findings([str(_csv(tmp_path / "e.csv", [{"name": "x"}]))])


# ── Presidio-gated ─────────────────────────────────────────────────────────


@pytest.mark.skipif(
    anonymize.preflight() is not None,
    reason="Presidio / fr_core_news_md not installed in this env",
)
def test_detect_flags_an_obvious_email_column(tmp_path):
    f = _csv(
        tmp_path / "clients.csv",
        [
            {"contact": "jean.dupont@example.fr", "city": "Lyon"},
            {"contact": "marie.sy@example.fr", "city": "Paris"},
            {"contact": "ada.lovelace@example.fr", "city": "Lille"},
        ],
    )
    findings = anonymize.detect_findings([str(f)])
    flagged = {(x.column) for x in findings}
    assert "contact" in flagged
    assert "city" not in flagged
