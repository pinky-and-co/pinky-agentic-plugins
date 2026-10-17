#!/usr/bin/env python3
"""Tests for `detect_findings` — column sampling, per-value entity voting, text fallback.

The analyzer-backed tests are gated on Presidio + the French spaCy model being importable;
the config-skip test must run everywhere (it asserts the model is never loaded).
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


@requires_model
def test_column_of_six_emails_is_flagged(tmp_path):
    """Six obvious emails out of six sampled values clear the vote threshold decisively."""
    f = _csv(
        tmp_path / "clients.csv",
        [{"contact": f"user{i}@example.fr", "note": "x"} for i in range(6)],
    )
    findings = anonymize.detect_findings([str(f)])
    contact = [x for x in findings if x.column == "contact"]
    assert contact, findings
    assert "EMAIL_ADDRESS" in contact[0].reason
    assert all(x.column != "note" for x in findings)


@requires_model
def test_sequential_nine_digit_ids_are_not_sirens(tmp_path):
    """Regression test (exact repro values): same-length business IDs are not SIRENs — the
    Luhn checksum must drop them, or every sequential `IDS` column false-positives."""
    f = _csv(
        tmp_path / "refs.csv",
        [
            {"IDS": v, "note": "x"}
            for v in ("331014438", "330610166", "330610167", "330610168", "330610169")
        ],
    )
    assert [x for x in anonymize.detect_findings([str(f)]) if x.column == "IDS"] == []


@requires_model
def test_luhn_valid_siren_and_siret_are_flagged(tmp_path):
    """Checksum-valid SIREN and SIRET values are flagged with their own entity types, under
    column names the keyword heuristic would not catch."""
    f = _csv(
        tmp_path / "companies.csv",
        [
            {"siren": "732 829 320", "siret": "73282932000074", "note": "x"},
            {"siren": "552 100 554", "siret": "55210055400013", "note": "y"},
            {"siren": "813 621 570", "siret": "81362157000007", "note": "z"},
        ],
    )
    findings = anonymize.detect_findings([str(f)])
    siren = [x for x in findings if x.column == "siren"]
    siret = [x for x in findings if x.column == "siret"]
    assert siren and "FR_SIREN" in siren[0].reason
    assert siret and "FR_SIRET" in siret[0].reason


@requires_model
def test_luhn_valid_credit_card_is_flagged_in_french(tmp_path):
    """CREDIT_CARD is registered for "fr" — a card column must be detected, not silently
    missed because the recognizer sat at its "en" default."""
    f = _csv(
        tmp_path / "payments.csv",
        [
            {"card": v, "note": "x"}
            for v in ("4111111111111111", "4242424242424242", "5555555555554444")
        ],
    )
    findings = anonymize.detect_findings([str(f)])
    card = [x for x in findings if x.column == "card"]
    assert card and "CREDIT_CARD" in card[0].reason


@requires_model
def test_mixed_column_flagged_by_sum_of_votes_not_top1(tmp_path):
    """A column holding one email, one phone and one person (plus neutral values) is judged
    on values carrying ANY maskable entity: with every type at one vote, a top-1 rule finds
    no type above the threshold and misses the column entirely."""
    f = _csv(
        tmp_path / "notes.csv",
        [
            {"info": v, "flag": "1"}
            for v in (
                "jean.dupont@example.fr",
                "06 12 34 56 78",
                "Marie Sy",
                "actif",
                "en cours",
                "solde",
            )
        ],
    )
    findings = anonymize.detect_findings([str(f)])
    assert any(x.column == "info" for x in findings), findings


def test_config_files_skip_detection_without_loading_the_model(tmp_path, monkeypatch):
    """Config files (by extension or by shape) return no findings AND never build the
    analyzer — this is the path every second `Read` takes, it has to stay cheap."""
    loaded = []
    monkeypatch.setattr(anonymize, "_build_analyzer", lambda: loaded.append(1))
    by_ext = tmp_path / "app.yml"
    by_ext.write_text("contact: jean@example.fr\n", encoding="utf-8")
    by_shape = tmp_path / "misc.txt"
    by_shape.write_text(
        "email: jean@example.fr\nphone: 06 12 34 56 78\n", encoding="utf-8"
    )
    assert anonymize.detect_findings([str(by_ext), str(by_shape)]) == []
    assert loaded == []


@requires_model
def test_positional_file_with_a_nir_flagged_via_text_fallback(tmp_path):
    """A NIR hidden inside a positional extract is flagged line-by-line, surfaced as a
    "(no columns — FR_NIR)" finding since the file has no column semantics."""
    p = tmp_path / "extract.dat"
    p.write_text(
        "CUST0001    2 88 07 75 116 001 32    ACTIF\n"
        "CUST0002    TOTAL 45.90             ACTIF\n",
        encoding="utf-8",
    )
    findings = anonymize.detect_findings([str(p)])
    assert any(x.column == "(no columns — FR_NIR)" for x in findings), findings


@requires_model
def test_bracketed_log_with_an_email_is_flagged_via_text_fallback(tmp_path):
    """A `[INFO]`-prefixed log is scanned line by line, not crashed into the json reader."""
    p = tmp_path / "app.log"
    p.write_text(
        "[INFO] 2026-01-01 contact jean.dupont@example.fr\n[WARN] disk low\n",
        encoding="utf-8",
    )
    findings = anonymize.detect_findings([str(p)])
    assert findings, findings
    assert any("EMAIL_ADDRESS" in x.column for x in findings)


def test_code_files_skip_detection_without_loading_the_model(tmp_path, monkeypatch):
    """A source file returns no findings and never builds the analyzer."""
    loaded = []
    monkeypatch.setattr(anonymize, "_build_analyzer", lambda: loaded.append(1))
    py = tmp_path / "mod.py"
    py.write_text('EMAIL = "jean@example.fr"\n', encoding="utf-8")
    assert anonymize.detect_findings([str(py)]) == []
    assert loaded == []


@requires_model
def test_free_text_with_only_person_names_is_not_flagged(tmp_path):
    """Names in free text are a guess by the NLP model, not a checkable format — prose (docs,
    logs, notes) must not raise an alert on `PERSON` alone."""
    p = tmp_path / "notes.txt"
    p.write_text(
        "Jean Dupont a rencontré Marie Curie à Lyon.\n"
        "Pierre Martin et Sophie Bernard ont signé le contrat.\n"
        "Paul Durand dirige l'équipe avec Claire Petit.\n",
        encoding="utf-8",
    )
    assert anonymize.detect_findings([str(p)]) == []


@requires_model
def test_free_text_with_a_strict_format_entity_is_still_flagged(tmp_path):
    """A checkable format (an email here) in free text is a real signal even with one hit."""
    p = tmp_path / "notes.txt"
    p.write_text(
        "Jean Dupont a rencontré Marie Curie à Lyon.\n"
        "Contact : jean.dupont@example.fr pour la suite.\n"
        "Paul Durand dirige l'équipe avec Claire Petit.\n",
        encoding="utf-8",
    )
    findings = anonymize.detect_findings([str(p)])
    assert [f.column for f in findings] == ["(no columns — EMAIL_ADDRESS)"]


@requires_model
def test_xml_date_tags_with_a_utc_offset_are_not_phone_columns(tmp_path):
    """A timestamp-like span can come back from Presidio as both DATE_TIME (0.6) and PHONE_NUMBER (0.4)
    — the lower-scored phone guess must not make a date tag look like PII."""
    body = "".join(
        f"<w:Worker><w:Hire_Date>2021-10-0{i}-07:00</w:Hire_Date>"
        f"<w:End_Date>2026-06-3{i % 2}-07:00</w:End_Date></w:Worker>"
        for i in range(1, 5)
    )
    p = tmp_path / "workers.xml"
    p.write_text(f"<w:Root>{body}</w:Root>", encoding="utf-8")
    assert anonymize.detect_findings([str(p)]) == []


@requires_model
def test_a_real_phone_number_column_is_still_flagged(tmp_path):
    """The date rule is scoped to DATE_TIME-vs-PHONE_NUMBER overlap; a genuine phone stays."""
    f = _csv(
        tmp_path / "phones.csv",
        [{"tel": "06 12 34 56 78", "note": "x"} for _ in range(4)],
    )
    findings = anonymize.detect_findings([str(f)])
    assert any(x.column == "tel" and "PHONE_NUMBER" in x.reason for x in findings)


_ANON_XML = (
    "<w:Root>"
    + "".join(
        f"<w:Worker><w:IBAN>IBAN_{i:08x}</w:IBAN><w:First_Name>FIRST_NAME_{i:08x}</w:First_Name>"
        "</w:Worker>"
        for i in range(1, 5)
    )
    + "</w:Root>"
)


@requires_model
def test_values_only_ignores_the_column_name_heuristic(tmp_path):
    """An anonymized copy keeps its tag names (`IBAN`, `First_Name`); with `values_only` they do
    not count, since only the values were replaced. Without it, the name match still flags."""
    p = tmp_path / "workers.anon.xml"
    p.write_text(_ANON_XML, encoding="utf-8")
    assert anonymize.detect_findings([str(p)], values_only=True) == []
    assert {x.column for x in anonymize.detect_findings([str(p)])} == {
        "IBAN",
        "First_Name",
    }


@requires_model
def test_values_only_still_flags_a_value_left_unanonymized(tmp_path):
    """A real email surviving in an `.anon` copy is still caught on its value."""
    p = tmp_path / "workers.anon.xml"
    p.write_text(
        "<w:Root>" + "<w:Mail>jean.dupont@example.fr</w:Mail>" * 4 + "</w:Root>",
        encoding="utf-8",
    )
    findings = anonymize.detect_findings([str(p)], values_only=True)
    assert [x.column for x in findings] == ["Mail"]


@requires_model
def test_all_caps_words_without_a_valid_country_code_are_not_bic(tmp_path):
    """A BIC's letters 5-6 are an ISO 3166 country. `APPROVED` (OV), `CREATION` (TI) and
    `REJECTED` (CT) have none; a real BIC in the same file is still flagged."""
    f = _csv(
        tmp_path / "status.csv",
        [
            {"state": "APPROVED", "bank": "CEPAFRPP450"},
            {"state": "CREATION", "bank": "BNPAFRPPXXX"},
            {"state": "REJECTED", "bank": "SOGEFRPP"},
        ],
    )
    flagged = {x.column for x in anonymize.detect_findings([str(f)])}
    assert "bank" in flagged
    assert "state" not in flagged


@requires_model
def test_values_only_skips_our_own_pseudonyms(tmp_path):
    """`LAST_NAME_a48ea4d9` is what `anonymize_apply` wrote: the model reads roughly one in ten as a
    person's name, which would make every anonymized copy unreadable through the guard."""
    p = tmp_path / "workers.anon.xml"
    p.write_text(
        "<w:Root>"
        + "".join(
            f"<w:Last_Name>LAST_NAME_{h}</w:Last_Name>"
            for h in ("a48ea4d9", "29edb228", "c18ee56d", "d0659f57")
        )
        + "</w:Root>",
        encoding="utf-8",
    )
    assert anonymize.detect_findings([str(p)], values_only=True) == []


_BICS = [
    "CEPAFRPP450",
    "BNPAFRPPXXX",
    "SOGEFRPP",
    "AGRIFRPP",
    "BNPAFRPP",
    "CCBPFRPP",
    "CMCIFRPP",
    "BPCEFRPP",
    "PSSTFRPP",
]


@requires_model
def test_a_word_inside_a_sentence_does_not_make_a_bic_column(tmp_path):
    """The value is `CREATION DE POSTE CDI`: the BIC pattern matches the word `CREATION`, not the
    value. Fixed-format identifiers only count when the match covers the whole value."""
    f = _csv(
        tmp_path / "contracts.csv",
        [{"reason": "CREATION DE POSTE CDI", "note": "x"} for _ in range(3)],
    )
    assert anonymize.detect_findings([str(f)]) == []


@requires_model
def test_identifiers_embedded_in_labels_do_not_flag_an_xml_tag(tmp_path):
    """`TRANSACTION` (a syntactically valid BIC) and a Luhn-valid 14-digit code, both inside a
    label, are substrings — not a BIC or a SIRET column."""
    labels = [
        "Business Line : TRANSACTION FRANCE",
        "Centre de coûts : 00177400000015 Orléans",
    ] * 2
    p = tmp_path / "worktags.xml"
    p.write_text(
        "<w:Root>"
        + "".join(f"<w:Worktag_Label>{v}</w:Worktag_Label>" for v in labels)
        + "</w:Root>",
        encoding="utf-8",
    )
    assert anonymize.detect_findings([str(p)]) == []


@requires_model
def test_a_bic_column_is_judged_on_a_conformity_ratio(tmp_path):
    """Profiling-style, 80 % threshold: 9 valid BICs and an `N/A` (90 %) and 4 of 5 (80 %) are BIC
    columns; 3 of 5 (60 %) is not."""

    def bank(values: list[str]) -> list[str]:
        f = _csv(tmp_path / "bank.csv", [{"c": v, "note": "x"} for v in values])
        return [x.column for x in anonymize.detect_findings([str(f)])]

    assert bank(_BICS + ["N/A"]) == ["c"]
    assert bank(_BICS[:4] + ["N/A"]) == ["c"]
    assert bank(_BICS[:3] + ["N/A", "n/a"]) == []


@requires_model
def test_an_identifier_with_underscores_is_not_a_person(tmp_path):
    """`COST_CENTER` and `CUSTOM_ORGANIZATION_WORKTAG_DIMENSION_02` are identifiers: a person's
    name has no underscore, whatever the NLP model guesses."""
    values = ["CUSTOM_ORGANIZATION_WORKTAG_DIMENSION_02", "COST_CENTER"] * 2
    p = tmp_path / "worktags.xml"
    p.write_text(
        "<w:Root>"
        + "".join(f"<w:Worktag_Dimension>{v}</w:Worktag_Dimension>" for v in values)
        + "</w:Root>",
        encoding="utf-8",
    )
    assert anonymize.detect_findings([str(p)]) == []


@requires_model
def test_a_column_of_real_person_names_is_still_flagged(tmp_path):
    """The underscore rule only drops identifiers; genuine names keep their PERSON vote."""
    f = _csv(
        tmp_path / "people.csv",
        [
            {"who": name, "note": "x"}
            for name in (
                "Jean Dupont",
                "Marie Curie",
                "Pierre Martin",
                "Sophie Bernard",
            )
        ],
    )
    findings = anonymize.detect_findings([str(f)])
    assert any(x.column == "who" and "PERSON" in x.reason for x in findings)


@requires_model
def test_text_scan_stops_at_the_line_budget_and_says_so(tmp_path):
    """A long free-text file is scanned line by line at ~3 ms a line: past the budget only the
    first lines are read, and the caller is told the rest was not analyzed."""
    p = tmp_path / "long.log"
    p.write_text(
        "".join(f"[INFO] line {i} ok\n" for i in range(50))
        + "[WARN] contact jean.dupont@example.fr\n",
        encoding="utf-8",
    )
    seen: list[bool] = []
    findings = anonymize.detect_findings(
        [str(p)], text_max_lines=20, truncated=lambda name: seen.append(True)
    )
    assert findings == []  # the email sits past line 20: not read
    assert seen == [True]
    # within the budget the same email is found
    assert anonymize.detect_findings([str(p)], text_max_lines=100)


@requires_model
def test_a_short_number_is_not_a_phone_but_real_numbers_are(tmp_path):
    """Presidio reads a bare `1640` as a phone number (0.4). A real one has at least 8 digits, in
    any of the usual spellings."""
    p = tmp_path / "requests.log"
    p.write_text(
        "".join(f"[INFO] request {n} handled ok\n" for n in range(1640, 1660)),
        encoding="utf-8",
    )
    assert anonymize.detect_findings([str(p)]) == []

    for spelled in ("06 12 34 56 78", "0612345678", "+33 6 12 34 56 78"):
        q = tmp_path / "contacts.log"
        q.write_text(f"[INFO] rappeler {spelled} demain\n" * 3, encoding="utf-8")
        flagged = anonymize.detect_findings([str(q)])
        assert any("PHONE_NUMBER" in f.column for f in flagged), spelled


@requires_model
def test_a_siret_column_is_not_reported_as_a_credit_card(tmp_path):
    """A Luhn-valid 14-digit SIRET also passes as a card number: the same span must be reported
    once, as the SIRET."""
    f = _csv(
        tmp_path / "companies.csv",
        [
            {"ref": "73282932000074", "note": "x"},
            {"ref": "55210055400013", "note": "y"},
            {"ref": "81362157000007", "note": "z"},
        ],
    )
    (finding,) = [x for x in anonymize.detect_findings([str(f)]) if x.column == "ref"]
    assert "FR_SIRET" in finding.reason
    assert "CREDIT_CARD" not in finding.reason


@requires_model
def test_siret_that_also_passes_as_a_card_is_named_siret_only(tmp_path):
    """SIRETs whose 14 digits also pass the card recognizer: no CREDIT_CARD, top or `also`."""
    sirets = ["44306184100047", "35600000000048", "73282932000074", "55210055400013"]
    f = _csv(tmp_path / "companies.csv", [{"ref": s, "note": "x"} for s in sirets])
    (finding,) = [x for x in anonymize.detect_findings([str(f)]) if x.column == "ref"]
    assert "FR_SIRET" in finding.reason
    assert "CREDIT_CARD" not in finding.reason


@requires_model
@pytest.mark.parametrize(
    "values",
    [
        ["44306184100047", "35600000000048", "73282932000074", "bonjour"],
        ["44306184100047", "35600000000048", "bonjour", "hello", "salut"],
    ],
)
def test_mixed_siret_column_stays_flagged_as_siret(tmp_path, values):
    """A SIRET-heavy mixed column is still flagged, FR_SIRET on top, never CREDIT_CARD."""
    f = _csv(tmp_path / "mixed.csv", [{"ref": v, "note": "x"} for v in values])
    (finding,) = [x for x in anonymize.detect_findings([str(f)]) if x.column == "ref"]
    assert finding.reason.startswith("detected FR_SIRET")
    assert "CREDIT_CARD" not in finding.reason


@requires_model
def test_real_card_numbers_column_stays_credit_card(tmp_path):
    """Genuine card numbers (not SIRET-shaped) keep the CREDIT_CARD verdict."""
    cards = ["4111111111111111", "5500005555555559", "378282246310005"]
    f = _csv(tmp_path / "cards.csv", [{"ref": c, "note": "x"} for c in cards])
    (finding,) = [x for x in anonymize.detect_findings([str(f)]) if x.column == "ref"]
    assert finding.reason.startswith("detected CREDIT_CARD")
