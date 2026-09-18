#!/usr/bin/env python3
"""Coherent, deterministic PII pseudonymization — the implementation behind the `pc-privacy` MCP server.

Two-phase design, no LLM in the loop (data never leaves the machine at this stage — LLMs are
unreliable at exhaustive/consistent PII masking; deterministic by default, an LLM only for the
analysis that follows):

    detect  — sample each column, run Presidio (French NLP engine) + a column-name heuristic
              backstop, return a report of columns judged sensitive. Writes nothing. Always run
              this first and have a human confirm/correct the column list before ``apply``.
    apply   — given a confirmed column list (matched by column name across ALL input files in the
              same call, so the SAME real value maps to the SAME pseudonym everywhere — preserving
              joins across tables), write an anonymized copy of each file next to the original
              (``<name>.anon.<ext>``).

Pseudonym = ``f"{COLUMN}_{blake2b(salt:column:value)[:8]}"`` — a pure function of (salt, column,
value), so the same real value always produces the same pseudonym within one ``apply`` call (across
every file passed to it) and across reruns that pass the same salt. A fresh salt is generated per
call and returned by :func:`apply_anonymization` (never written to disk) unless one is pinned.

Masked by default: PERSON, EMAIL_ADDRESS, PHONE_NUMBER, IBAN_CODE, CREDIT_CARD, FR_SIRET, FR_SIREN,
FR_NIR, BIC_SWIFT (FR_SIRET/FR_SIREN/FR_NIR/BIC_SWIFT are custom pattern recognizers — Presidio has
no built-in for them). DATE_TIME and generic LOCATION are deliberately left alone: they usually
carry the debugging signal itself (order dates, shipping city) and masking them by default would
cripple the analysis this exists for.

Formats: CSV and JSON (array-of-objects) via stdlib only. Parquet via a lazy ``pyarrow`` import. XML
via regex on leaf-tag text content (no parser — sidesteps the undeclared-namespace-prefix
``ParseError`` common in Workday PECI exports); the column list becomes tag *local names* (namespace
prefix stripped) instead of column names, everything else identical, same pseudonym scheme.

Dual-use: importable by ``mcp/server.py`` (which calls these functions and composes its own return
strings — never prints from here, stdout is the JSON-RPC channel over stdio) and runnable as a
standalone CLI via :func:`main` (which does print).

Usage (CLI):
    anonymize.py detect FILE [FILE ...]
    anonymize.py apply FILE [FILE ...] --columns col1,col2,... [--salt SALT]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import secrets
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Windows' default console codepage (cp1252, not UTF-8) can't encode this script's ✓/⚠ output.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

FR_SIRET_PATTERN = r"\b\d{3}\s?\d{3}\s?\d{3}\s?\d{5}\b"
FR_SIREN_PATTERN = r"\b\d{3}\s?\d{3}\s?\d{3}\b"
# NIR (numéro de sécurité sociale / INSEE) — sexe(1) AA(2) MM(2) dept(2-3) commune(2-3) ordre(3) clé(2),
# séparateurs espace ou tiret facultatifs entre chaque groupe.
FR_NIR_PATTERN = (
    r"\b[12][\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{2,3}[\s-]?\d{2,3}[\s-]?\d{3}[\s-]?\d{2}\b"
)
# BIC/SWIFT (ISO 9362) — code banque(4) + pays(2) + localisation(2) + code agence optionnel(3).
BIC_SWIFT_PATTERN = r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?\b"

SPACY_MODEL = "fr_core_news_md"

DEFAULT_MASK_ENTITIES = {
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "CREDIT_CARD",
    "FR_SIRET",
    "FR_SIREN",
    "FR_NIR",
    "BIC_SWIFT",
}

NAME_HEURISTIC_KEYWORDS = {
    "email",
    "mail",
    "courriel",
    "nom",
    "prenom",
    "prénom",
    "name",
    "telephone",
    "téléphone",
    "tel",
    "phone",
    "adresse",
    "address",
    "iban",
    "siret",
    "siren",
    "ssn",
    "secu",
    "sécu",
    "numero_secu",
    "national",
    "insee",
    "nir",
    "bic",
    "swift",
    "postal",
    "code_postal",
    "cp",
    "birth",
    "naissance",
    "account",
    "compte",
}

SAMPLE_SIZE = 15

# Leaf tag = no nested `<` in its content — a container (e.g. <Legal_Name><First_Name>…) never
# matches `[^<]*` and is correctly skipped; namespace prefix (peci:, ptdf:, …) stripped to local name.
_XML_LEAF_TAG_RE = re.compile(
    r"<(?:[\w-]+:)?([A-Za-z_][\w.-]*)\b[^>]*>([^<]*)</(?:[\w-]+:)?\1>"
)


class AnonymizeError(Exception):
    """A caller-facing error (bad file, missing dependency) — the server turns it into a status line."""


@dataclass(frozen=True)
class Finding:
    """One column/tag judged sensitive. `reason` names WHY (entity type + votes, or keyword match) —
    it never carries a sampled value, so a report built from Findings cannot leak real PII."""

    file: str
    column: str
    reason: str


@dataclass(frozen=True)
class ApplyOutcome:
    """Result of one `apply` run — output paths, how many columns each file matched, the salt used,
    and the count of distinct real values pseudonymized. Never carries a value or a pseudonym."""

    outputs: list[
        tuple[str, str, int]
    ]  # (input name, output name, columns/tags masked)
    salt: str
    unique_values: int
    skipped: list[str]  # files where none of the requested columns were present


# ── spaCy / Presidio availability ────────────────────────────────────────────


def spacy_model_available() -> bool:
    """True if the French spaCy model needed by Presidio is importable. Never raises."""
    try:
        import importlib.util

        return importlib.util.find_spec(SPACY_MODEL) is not None
    except (ImportError, ValueError):
        return False


def preflight() -> str | None:
    """Return a human hint if Presidio or the spaCy model is missing, else None. Never raises.

    Returns:
        A one-line remediation string, or None when `detect` can run.
    """
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        return (
            "presidio-analyzer is not installed — the MCP server resolves it via `uv run --script`; "
            "run the server through that entrypoint rather than importing this module bare."
        )
    if not spacy_model_available():
        return (
            f"spaCy model `{SPACY_MODEL}` not installed. First-time setup, run once: "
            f"`uv run --with spacy python -m spacy download {SPACY_MODEL}` (large download, ~45 MB)."
        )
    return None


def _build_analyzer():  # type: ignore[no-untyped-def]  # noqa: DAR201
    """Presidio AnalyzerEngine on the French spaCy model + custom SIRET/SIREN pattern recognizers.

    Returns:
        A configured presidio_analyzer.AnalyzerEngine (untyped — Presidio ships no stubs).

    Raises:
        AnonymizeError: If Presidio or the spaCy model is unavailable.
    """
    hint = preflight()
    if hint:
        raise AnonymizeError(hint)
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "fr", "model_name": SPACY_MODEL}],
        }
    )
    analyzer = AnalyzerEngine(
        nlp_engine=provider.create_engine(), supported_languages=["fr"]
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="FR_SIRET",
            patterns=[Pattern("siret", FR_SIRET_PATTERN, 0.6)],
        )
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="FR_SIREN",
            patterns=[Pattern("siren", FR_SIREN_PATTERN, 0.4)],
        )
    )
    # supported_language="fr" required: an "en" recognizer (the default) on an "fr"-only
    # `analyze()` call is silently never queried.
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="FR_NIR",
            supported_language="fr",
            patterns=[Pattern("nir", FR_NIR_PATTERN, 0.6)],
        )
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="BIC_SWIFT",
            supported_language="fr",
            patterns=[Pattern("bic_swift", BIC_SWIFT_PATTERN, 0.5)],
            # PatternRecognizer's global_regex_flags default to IGNORECASE, which would turn this
            # uppercase-only ISO-9362 pattern into a case-insensitive match against any ordinary
            # 8- or 11-letter word (e.g. "lovelace") — drop it, BIC/SWIFT codes are uppercase by spec.
            global_regex_flags=re.DOTALL | re.MULTILINE,
        )
    )
    return analyzer


# ── file IO ──────────────────────────────────────────────────────────────────


def _read_rows(path: Path) -> tuple[list[str], list[dict]]:
    """Load a CSV/JSON (array-of-objects)/Parquet file into (fieldnames, rows).

    Args:
        path: The file to read — extension decides the parser.

    Returns:
        (fieldnames, rows) — column order and a list of row dicts.

    Raises:
        AnonymizeError: On an unsupported extension, a non-array JSON, or missing pyarrow.
    """
    if path.suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            return list(reader.fieldnames or []), rows
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise AnonymizeError(f"{path.name}: expected a JSON array of objects")
        fieldnames = list(data[0].keys()) if data else []
        return fieldnames, data
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as e:
            raise AnonymizeError(
                "Parquet support needs pyarrow (install it into the server env)"
            ) from e
        table = pq.read_table(path)
        return list(table.column_names), table.to_pylist()
    raise AnonymizeError(
        f"{path.name}: unsupported extension (csv/json/parquet/xml only)"
    )


def _write_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> Path:
    out = path.with_suffix(f".anon{path.suffix}")
    if path.suffix == ".csv":
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    elif path.suffix == ".json":
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif path.suffix == ".parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq

        pq.write_table(pa.Table.from_pylist(rows), out)
    else:
        # Unreachable via apply_anonymization (_read_rows already rejects the suffix) — a guard so
        # the invariant lives in the function, not in a caller relationship.
        raise AnonymizeError(f"{path.name}: cannot write, unsupported extension")
    return out


def _xml_leaf_samples(text: str) -> dict[str, list[str]]:
    """tag local name -> sample of its non-empty leaf text values (container tags never match)."""
    samples: dict[str, list[str]] = {}
    for tag, value in _XML_LEAF_TAG_RE.findall(text):
        value = value.strip()
        if not value:
            continue
        bucket = samples.setdefault(tag, [])
        if len(bucket) < SAMPLE_SIZE:
            bucket.append(value)
    return samples


# ── detect ───────────────────────────────────────────────────────────────────


def _column_samples(path: Path) -> dict[str, list[str]]:
    """Column/tag name -> up to SAMPLE_SIZE non-empty sampled values, for any supported format."""
    if path.suffix == ".xml":
        return _xml_leaf_samples(path.read_text(encoding="utf-8", errors="ignore"))
    fieldnames, rows = _read_rows(path)
    return {
        col: [str(r[col]) for r in rows if r.get(col)][:SAMPLE_SIZE]
        for col in fieldnames
    }


def detect_findings(files: list[str]) -> list[Finding]:
    """Run Presidio + the name heuristic over sampled values of every column/tag in `files`.

    Args:
        files: Paths to CSV/JSON/Parquet/XML files.

    Returns:
        One Finding per column/tag judged sensitive, across all files.

    Raises:
        AnonymizeError: If Presidio/the spaCy model is missing, or a file cannot be read.
    """
    analyzer = _build_analyzer()
    findings: list[Finding] = []
    for fp in files:
        path = Path(fp)
        for name, values in _column_samples(path).items():
            if not values:
                continue
            name_hit = any(k in name.lower() for k in NAME_HEURISTIC_KEYWORDS)
            votes: Counter[str] = Counter()
            for v in values:
                for result in analyzer.analyze(text=v, language="fr"):
                    votes[result.entity_type] += 1
            top_entity, count = (votes.most_common(1) or [("", 0)])[0]
            is_pii = top_entity in DEFAULT_MASK_ENTITIES and count >= max(
                2, len(values) // 3
            )
            if is_pii:
                findings.append(
                    Finding(
                        path.name,
                        name,
                        f"detected {top_entity} ({count}/{len(values)})",
                    )
                )
            elif name_hit:
                findings.append(
                    Finding(path.name, name, "column/tag name matches a PII keyword")
                )
    return findings


def format_detect_report(findings: list[Finding]) -> str:
    """Render `findings` as a fixed-width table. Pure formatter — no PII, no file access.

    Args:
        findings: Output of :func:`detect_findings`.

    Returns:
        A multi-line report ending with the next-step instruction.
    """
    lines = [f"{'file':<28} {'column/tag':<24} verdict   reason", "-" * 88]
    if findings:
        for f in findings:
            lines.append(f"{f.file:<28} {f.column:<24} SENSITIVE {f.reason}")
    else:
        lines.append(
            "(no column/tag flagged — verify manually before assuming the dataset is clean)"
        )
    lines.append("")
    lines.append(
        "Review the list. Confirm the column names, then call `apply` with them — nothing written yet."
    )
    return "\n".join(lines)


# ── apply ────────────────────────────────────────────────────────────────────


def _make_pseudonymizer(salt: str) -> tuple[Callable[[str, str], str], dict]:
    """Build a (column, value) -> pseudonym function backed by a shared, growing mapping."""
    mapping: dict[tuple[str, str], str] = {}

    def pseudonym(col: str, value: str) -> str:
        key = (col, value)
        if key not in mapping:
            digest = hashlib.blake2b(
                f"{salt}:{col}:{value}".encode(), digest_size=4
            ).hexdigest()
            mapping[key] = f"{col.upper()}_{digest}"
        return mapping[key]

    return pseudonym, mapping


def _mask_xml(
    path: Path, tags: set[str], pseudonym: Callable[[str, str], str]
) -> tuple[Path, int]:
    """Regex-substitute leaf-tag text content for tags in `tags`. Returns (output path, tags masked)."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    hit_tags: set[str] = set()

    def repl(m: re.Match) -> str:
        tag, value = m.group(1), m.group(2)
        if tag not in tags or not value.strip():
            return m.group(0)
        hit_tags.add(tag)
        open_tag, _, close_tag = m.group(0).partition(value)
        return f"{open_tag}{pseudonym(tag, value.strip())}{close_tag}"

    masked = _XML_LEAF_TAG_RE.sub(repl, text)
    out = path.with_suffix(".anon.xml")
    out.write_text(masked, encoding="utf-8")
    return out, len(hit_tags)


def apply_anonymization(
    files: list[str], columns: list[str], salt: str | None = None
) -> ApplyOutcome:
    """Write an anonymized copy of each file, pseudonymizing `columns` coherently across all of them.

    Args:
        files: Paths to CSV/JSON/Parquet/XML files.
        columns: Column names (CSV/JSON/Parquet) or tag local names (XML) to mask. Order and
            duplicates are irrelevant.
        salt: Pin the salt to keep several runs joinable. Default: a fresh random salt, returned
            in the outcome so the caller can reuse it.

    Returns:
        An ApplyOutcome with the output paths, the salt used, and counts — never a value.

    Raises:
        AnonymizeError: If a file cannot be read or written.
    """
    salt = salt or secrets.token_hex(8)
    wanted = set(columns)
    pseudonym, mapping = _make_pseudonymizer(salt)
    outputs: list[tuple[str, str, int]] = []
    skipped: list[str] = []

    for fp in files:
        path = Path(fp)
        if path.suffix == ".xml":
            out, n_hit = _mask_xml(path, wanted, pseudonym)
            if not n_hit:
                skipped.append(path.name)
            outputs.append((path.name, out.name, n_hit))
            continue
        fieldnames, rows = _read_rows(path)
        hit_columns = [c for c in fieldnames if c in wanted]
        if not hit_columns:
            skipped.append(path.name)
        for row in rows:
            for col in hit_columns:
                if row.get(col):
                    row[col] = pseudonym(col, str(row[col]))
        out = _write_rows(path, fieldnames, rows)
        outputs.append((path.name, out.name, len(hit_columns)))

    return ApplyOutcome(
        outputs=outputs,
        salt=salt,
        unique_values=len(mapping),
        skipped=skipped,
    )


def format_apply_report(outcome: ApplyOutcome) -> str:
    """Render an ApplyOutcome as a status block. No PII — output filenames + counts + the salt.

    Args:
        outcome: Output of :func:`apply_anonymization`.

    Returns:
        A multi-line status string, including the salt to reuse for joinable reruns.
    """
    lines = []
    for src, dst, n in outcome.outputs:
        # n == 0 is exactly the skipped condition (no column/tag matched) — a per-row flag, so two
        # inputs sharing a basename can't cross-contaminate the way a name lookup in `skipped` would.
        note = (
            " (none of the requested columns present, copied as-is)" if n == 0 else ""
        )
        lines.append(f"{src} -> {dst} · {n} column(s)/tag(s) masked{note}")
    lines.append("")
    lines.append(
        f"{outcome.unique_values} distinct value(s) pseudonymized across {len(outcome.outputs)} file(s)."
    )
    lines.append(
        f"salt: {outcome.salt}  — pass this salt to a later `apply` to keep the pseudonyms joinable."
    )
    return "\n".join(lines)


# ── CLI (standalone use only — the MCP server never calls this) ───────────────


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Prints (unlike the importable functions above)."""
    ap = argparse.ArgumentParser(
        description="Deterministic, coherent PII pseudonymization."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_detect = sub.add_parser(
        "detect", help="report PII-looking columns, write nothing"
    )
    p_detect.add_argument("files", nargs="+")

    p_apply = sub.add_parser(
        "apply", help="anonymize confirmed columns, write <name>.anon.<ext>"
    )
    p_apply.add_argument("files", nargs="+")
    p_apply.add_argument(
        "--columns", required=True, help="comma-separated column names"
    )
    p_apply.add_argument(
        "--salt", default=None, help="pin the salt for joinable reruns"
    )

    args = ap.parse_args(argv)
    try:
        if args.cmd == "detect":
            print(format_detect_report(detect_findings(args.files)))
            return 0
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]
        outcome = apply_anonymization(args.files, columns, args.salt)
        print(format_apply_report(outcome))
        return 0
    except AnonymizeError as e:
        print(f"[anonymize] BLOCKED — {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
