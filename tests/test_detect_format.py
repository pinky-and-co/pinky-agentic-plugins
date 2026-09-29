#!/usr/bin/env python3
"""Tests for `detect_format` — content-based format classification.

None of these need Presidio: `detect_format` decides from the `file` signature and the head
bytes only, and never builds the analyzer.
"""

from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "mcp"))

import anonymize


def test_config_extension_wins_over_dataset_looking_content(tmp_path):
    """A known config extension is never scanned as a dataset, whatever its content holds."""
    csv_body = "contact,city\njean@example.fr,Lyon\nmarie@example.fr,Paris\n"
    for ext in (
        ".yml",
        ".yaml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".properties",
    ):
        p = tmp_path / f"conf{ext}"
        p.write_text(csv_body, encoding="utf-8")
        assert anonymize.detect_format(p) == "config", ext


def test_single_json_object_is_config_array_of_objects_is_json(tmp_path):
    """One JSON object (pretty-printed or not) is config-shaped; an array of objects is a dataset."""
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"host": "db", "port": 5432}), encoding="utf-8")
    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps({"host": "db"}, indent=2) + "\n", encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text(json.dumps([{"a": "1"}, {"a": "2"}]), encoding="utf-8")
    assert anonymize.detect_format(flat) == "config"
    assert anonymize.detect_format(pretty) == "config"
    assert anonymize.detect_format(array) == "json"


def test_jsonl_object_lines_detected(tmp_path):
    """Two or more lines, each a JSON object, make a JSONL dataset."""
    p = tmp_path / "events.jsonl"
    p.write_text('{"event": "click"}\n{"event": "view"}\n', encoding="utf-8")
    assert anonymize.detect_format(p) == "jsonl"


@pytest.mark.parametrize("delim", [",", ";"])
def test_csv_detected_and_parsed_with_the_sniffed_delimiter(tmp_path, delim):
    """Both `,` and `;` CSVs classify as csv, and `_read_rows` uses the sniffed delimiter —
    a French `;` export read with the stdlib comma default would come back as one column."""
    p = tmp_path / "export.csv"
    p.write_text(f"a{delim}b\n1{delim}2\n3{delim}4\n", encoding="utf-8")
    assert anonymize.detect_format(p) == "csv"
    fieldnames, rows = anonymize._read_rows(p, "csv")
    assert fieldnames == ["a", "b"]
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_xml_detected_via_the_file_signature(tmp_path):
    """A declared XML document is recognized by `file`'s own signature, extension aside."""
    p = tmp_path / "feed.xml"
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<peci:Employee><peci:First_Name>Jean</peci:First_Name></peci:Employee>",
        encoding="utf-8",
    )
    assert anonymize.detect_format(p) == "xml"


def test_xml_pure_content_fallback_when_file_binary_is_absent(tmp_path, monkeypatch):
    """Without the `file` binary, the `<?xml` declaration alone still classifies as xml."""
    monkeypatch.setattr(anonymize.shutil, "which", lambda _cmd: None)
    p = tmp_path / "feed.xml"
    p.write_text('<?xml version="1.0"?><root><a>1</a></root>', encoding="utf-8")
    assert anonymize.detect_format(p) == "xml"


def test_xlsx_detected_by_content_even_with_an_exotic_extension(tmp_path):
    """A real openpyxl workbook classifies as xlsx whatever its extension — `file` names the
    spreadsheetml mime, and a `file`-less environment still catches the PK zip signature."""
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "extract.blob"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "nom"
    wb.active["A2"] = "Jean"
    wb.save(p)
    assert anonymize.detect_format(p) == "xlsx"


def test_key_value_majority_text_is_config_without_any_known_extension(tmp_path):
    """`key: value` and `key=value` majorities are config-shaped with no extension hint at all."""
    colon = tmp_path / "notes.txt"
    colon.write_text("host: example\nport: 5432\nuser: jean\n", encoding="utf-8")
    equals = tmp_path / "settings.dat"
    equals.write_text("HOST=x\nPORT=5432\nUSER=jean\n", encoding="utf-8")
    assert anonymize.detect_format(colon) == "config"
    assert anonymize.detect_format(equals) == "config"


def test_positional_extract_falls_back_to_text(tmp_path):
    """A fixed-width extract — no delimiter to sniff, no config shape — scans as plain text."""
    p = tmp_path / "extract.dat"
    p.write_text(
        "CUST0001    2 88 07 75 116 001 32    ACTIF\n"
        "CUST0002    TOTAL 45.90             ACTIF\n",
        encoding="utf-8",
    )
    assert anonymize.detect_format(p) == "text"


def test_pdf_png_docx_are_binary_never_datasets(tmp_path):
    """Real binary documents (pdf, image, docx) never reach Presidio as text."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<</Length 2>>stream\n\x00\x01"
        b"\nendstream\nendobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    png = tmp_path / "img.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 40 + b"IEND\xaeB`\x82"
    )
    docx = tmp_path / "report.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package'
            '/2006/content-types"><Default Extension="rels" ContentType="application/xml"/>'
            "</Types>",
        )
        # `_rels/.rels` is what makes `file` name the wordprocessingml mime rather than a
        # generic zip — every real docx carries one.
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="ns"><w:body/></w:document>',
        )
    assert anonymize.detect_format(pdf) == "binary"
    assert anonymize.detect_format(png) == "binary"
    assert anonymize.detect_format(docx) == "binary"


def test_unreadable_file_raises_instead_of_being_skipped(tmp_path):
    """A file the hook cannot read is an error, not a silent skip — BSD `file` prints its
    "cannot open" line on stdout with exit 0, which must not pass for a mime (a "binary"
    verdict here would silently allow an unreadable file through the guard)."""
    p = tmp_path / "locked.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    p.chmod(0)
    try:
        with pytest.raises(anonymize.AnonymizeError, match="cannot read"):
            anonymize.detect_format(p)
    finally:
        p.chmod(0o644)


def test_csv_sniffing_does_not_read_beyond_the_head(tmp_path):
    """Classification reads the head only — the property the `_read_rows` path relies on to
    stay cheap on big files: a multi-MB csv is still named from its first bytes."""
    p = tmp_path / "big.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["a", "b"])
        for i in range(200_000):
            w.writerow([str(i), "x"])
    assert anonymize.detect_format(p) == "csv"


def test_bracketed_log_is_text_not_json(tmp_path):
    """A log whose lines start with `[LEVEL]` opens with `[` but is not JSON — it must fall
    through to the text scan, not be routed to the json reader."""
    p = tmp_path / "app.log"
    p.write_text("[INFO] 2026-01-01 started\n[WARN] disk low\n", encoding="utf-8")
    assert anonymize.detect_format(p) == "text"


def test_json_array_of_scalars_is_not_routed_to_json(tmp_path):
    """`[1, 2, 3]` parses as JSON but holds no objects — not routed to the json reader."""
    p = tmp_path / "scalars.json"
    p.write_text("[1, 2, 3]\n", encoding="utf-8")
    assert anonymize.detect_format(p) != "json"


def test_corrupt_jsonl_line_past_the_head_raises_anonymize_error(tmp_path):
    """The head check sees 30 lines; a corrupt line beyond them must surface as `AnonymizeError`
    from the reader, never a raw `JSONDecodeError`."""
    p = tmp_path / "rows.jsonl"
    lines = [json.dumps({"n": i}) for i in range(40)]
    lines[35] = "{not json"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert anonymize.detect_format(p) == "jsonl"
    with pytest.raises(anonymize.AnonymizeError):
        anonymize._read_rows(p, "jsonl")


def test_json_array_of_scalars_read_rows_raises_anonymize_error(tmp_path):
    """Forced through the row reader, a non-object array is an `AnonymizeError`."""
    p = tmp_path / "scalars.json"
    p.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(anonymize.AnonymizeError):
        anonymize._read_rows(p, "json")


def test_source_and_doc_extensions_are_code_never_scanned(tmp_path):
    """Source/doc files are not datasets, whatever their content — `.sql` stays out of this
    set because a data dump (`INSERT ... VALUES`) is a real PII carrier."""
    for ext in (".py", ".js", ".ts", ".sh", ".md", ".rst"):
        p = tmp_path / f"file{ext}"
        p.write_text(
            "a,b\njean@example.fr,Lyon\nmarie@example.fr,Paris\n", encoding="utf-8"
        )
        assert anonymize.detect_format(p) == "code", ext
    sql = tmp_path / "dump.sql"
    sql.write_text("INSERT INTO t VALUES ('jean@example.fr');\n", encoding="utf-8")
    assert anonymize.detect_format(sql) != "code"
