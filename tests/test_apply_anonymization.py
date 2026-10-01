#!/usr/bin/env python3
"""Tests for `apply_anonymization` — text fallback masking, xlsx sheets, pseudonym coherence,
config skip. Complements `test_anonymize.py`'s coherence tests with the formats and trust
models added by the two-phase redesign.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "mcp"))

import anonymize

requires_model = pytest.mark.skipif(
    anonymize.preflight() is not None,
    reason="Presidio / fr_core_news_md not installed in this env",
)


def _csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def _read_column(path: Path, column: str) -> list[str]:
    with path.open(encoding="utf-8", newline="") as f:
        return [row[column] for row in csv.DictReader(f)]


@requires_model
def test_text_fallback_masks_each_line_to_the_same_length(tmp_path):
    """The positional fallback replaces matched spans with same-length fillers, so a
    fixed-width extract's external column boundaries survive the masking untouched."""
    p = tmp_path / "extract.dat"
    p.write_text(
        "CUST0001    2 88 07 75 116 001 32    ACTIF\n"
        "CUST0002    TOTAL 45.90             ACTIF\n",
        encoding="utf-8",
    )
    anonymize.apply_anonymization([str(p)], [], salt="s")
    masked = (tmp_path / "extract.anon.dat").read_text(encoding="utf-8").splitlines()
    original = p.read_text(encoding="utf-8").splitlines()
    assert [len(line) for line in masked] == [len(line) for line in original]
    assert "2 88 07 75 116 001 32" not in masked[0]


def test_xlsx_masks_only_the_confirmed_sheet_columns(tmp_path):
    """Apply touches exactly the confirmed `Sheet!Column` entries: the named column in the
    named sheet is pseudonymized, other columns and other sheets come through intact."""
    openpyxl = pytest.importorskip("openpyxl")
    src = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    clients = wb.active
    clients.title = "Clients"
    clients.append(["nom", "montant"])
    clients.append(["Jean Dupont", 10])
    clients.append(["Marie Sy", 20])
    sales = wb.create_sheet("Ventes")
    sales.append(["vendeur", "total"])
    sales.append(["Jean Dupont", 30])
    wb.save(src)

    anonymize.apply_anonymization([str(src)], ["Clients!nom"], salt="s")

    out = openpyxl.load_workbook(tmp_path / "book.anon.xlsx")
    clients_rows = list(out["Clients"].iter_rows(values_only=True))
    sales_rows = list(out["Ventes"].iter_rows(values_only=True))
    assert clients_rows[1][0].startswith("CLIENTS!NOM_")
    assert clients_rows[2][0].startswith("CLIENTS!NOM_")
    assert (
        clients_rows[1][0] != clients_rows[2][0]
    )  # distinct people, distinct pseudonym
    assert clients_rows[1][1] == 10  # unconfirmed column of the same sheet intact
    assert sales_rows[1][0] == "Jean Dupont"  # other sheet intact


def test_same_salt_across_calls_yields_the_same_pseudonym(tmp_path):
    """Two separate `apply` calls with the same salt produce the same pseudonym for the same
    value (reruns stay joinable), and a different salt produces a different one."""
    a = _csv(tmp_path / "a.csv", [{"customer": "Jean Dupont", "amount": "1"}])
    b = _csv(tmp_path / "b.csv", [{"customer": "Jean Dupont", "amount": "2"}])
    c = _csv(tmp_path / "c.csv", [{"customer": "Jean Dupont", "amount": "3"}])
    anonymize.apply_anonymization([str(a)], ["customer"], salt="pin")
    anonymize.apply_anonymization([str(b)], ["customer"], salt="pin")
    anonymize.apply_anonymization([str(c)], ["customer"], salt="other")
    pa = _read_column(tmp_path / "a.anon.csv", "customer")[0]
    pb = _read_column(tmp_path / "b.anon.csv", "customer")[0]
    pc = _read_column(tmp_path / "c.anon.csv", "customer")[0]
    assert pa == pb
    assert pa != pc


def test_config_file_lands_in_skipped_and_writes_no_anon_copy(tmp_path):
    """A config file passed to apply is reported skipped — no `.anon` file, original intact."""
    conf = tmp_path / "app.yml"
    conf.write_text("email: jean@example.fr\n", encoding="utf-8")
    outcome = anonymize.apply_anonymization([str(conf)], ["email"], salt="s")
    assert outcome.skipped == ["app.yml"]
    assert outcome.outputs == []
    assert not (tmp_path / "app.anon.yml").exists()
    assert conf.read_text(encoding="utf-8") == "email: jean@example.fr\n"
