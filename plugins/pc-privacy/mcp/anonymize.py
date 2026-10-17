#!/usr/bin/env python3
"""Deterministic, coherent PII pseudonymization — the implementation behind the `pc-privacy` MCP server.

No LLM in the loop: data never leaves the machine at this stage.

    detect  — sample each column, run Presidio (French NLP) + a column-name heuristic, report the
            columns judged sensitive. Writes nothing; a human confirms the list before ``apply``.
    apply   — pseudonymize the confirmed columns (matched by name across ALL files of the call, so
            joins survive) and write ``<n>.anon.<ext>`` next to each original.

Pseudonym = ``f"{COLUMN}_{blake2b(salt:column:value)[:8]}"``: a pure function of (salt, column,
value). A fresh salt is generated per call and returned (never written to disk) unless pinned.

Masked by default: PERSON, EMAIL_ADDRESS, PHONE_NUMBER, IBAN_CODE, CREDIT_CARD, FR_SIRET, FR_SIREN,
FR_NIR, BIC_SWIFT. DATE_TIME and LOCATION are left alone: they usually carry the debugging signal.

Formats, decided by content rather than extension (:func:`detect_format`): CSV, JSON (array of
objects), JSONL, Parquet (``pyarrow``), XLSX (``openpyxl``, columns named ``Sheet!Column``), XML
(regex on leaf tags, names are tag local names). Config, code and binary files are skipped.
Unrecognized tabular shapes (e.g. fixed-width) are scanned line by line and masked automatically
with a same-length filler, with no salt and no column confirmation.

Importable by ``mcp/server.py`` (never prints: stdout is the JSON-RPC channel) and runnable as a
CLI via :func:`main`.

Usage (CLI):
    anonymize.py detect FILE [FILE ...]
    anonymize.py apply FILE [FILE ...] --columns col1,col2,... [--salt SALT]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Console Windows (cp1252) : forcer UTF-8 en sortie.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

FR_SIRET_PATTERN = r"\b\d{3}\s?\d{3}\s?\d{3}\s?\d{5}\b"
FR_SIREN_PATTERN = r"\b\d{3}\s?\d{3}\s?\d{3}\b"
# NIR : clé (mod 97) et préfixe NIA (7/8) non validés — recall privilégié sur precision.
FR_NIR_PATTERN = r"\b[12][\s-]?\d{2}[\s-]?\d{2}[\s-]?(?:\d{2,3}|2[AB])[\s-]?\d{2,3}[\s-]?\d{3}[\s-]?\d{2}\b"
# BIC/SWIFT (ISO 9362) : banque(4) + pays(2) + localisation(2) + agence(3, optionnel).
BIC_SWIFT_PATTERN = r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?\b"
# ISO 3166-1 alpha-2 (+ XK, Kosovo) : lettres 5-6 d'un vrai BIC, filtre les mots en majuscules.
_ISO_COUNTRIES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS
    BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE
    EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM
    HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC
    LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA
    NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
    TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW
    """.split()
)


# SIREN/SIRET : checksum Luhn (mod 10) exigé par l'INSEE, ce qui filtre les identifiants de même
# longueur. Exception connue, non gérée : les numéros historiques de La Poste échouent au Luhn.
def _luhn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


SPACY_MODEL = "fr_core_news_md"

# GLiNER (opt-in) s'ajoute à spaCy, qui rate les noms en majuscules. Requiert
# `presidio-analyzer[gliner]` et un téléchargement de modèle.
#   PC_PRIVACY_NER=gliner | PC_PRIVACY_GLINER_MODEL=<id ou chemin> | PC_PRIVACY_GLINER_THRESHOLD=0.3
GLINER_DEFAULT_MODEL = "urchade/gliner_multi_pii-v1"
GLINER_ENTITY_MAPPING = {
    "person": "PERSON",
    "name": "PERSON",
    "organization": "ORGANIZATION",
    "location": "LOCATION",
}

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
    "birth",
    "naissance",
    "account",
    "compte",
}

SAMPLE_SIZE = 15

# Formats de config jamais scannés, quel que soit le contenu (un JSON-objet : cf. detect_format).
CONFIG_EXTENSIONS = {
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".properties",
}

# Code et documentation, jamais scannés. `.sql` volontairement absent : un dump contient du PII.
CODE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".zsh",
    ".bash",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".rb",
    ".php",
    ".swift",
    ".css",
    ".scss",
    ".md",
    ".rst",
    ".ipynb",
    ".lock",
}

# Ligne de config `clé: valeur` / `clé=valeur` ; le « : » exige un espace (`12:30,` est une heure).
_KV_LINE_RE = re.compile(r"^[ \t]*[\w.\-]+[ \t]*(?:=[ \t]*\S|:[ \t]+\S)")

# Balise feuille (aucun `<` imbriqué) ; le préfixe de namespace est retiré.
_XML_LEAF_TAG_RE = re.compile(
    r"<(?:[\w-]+:)?([A-Za-z_][\w.-]*)\b[^>]*>([^<]*)</(?:[\w-]+:)?\1>"
)


class AnonymizeError(Exception):
    """Caller-facing error (bad file, missing dependency)."""


@dataclass(frozen=True)
class Finding:
    """One column/tag judged sensitive. `reason` never carries a sampled value."""

    file: str
    column: str
    reason: str


@dataclass(frozen=True)
class ApplyOutcome:
    """Result of one `apply` run: outputs, salt, distinct-value count. Never carries a value."""

    outputs: list[tuple[str, str, int]]  # (entrée, sortie, colonnes/balises masquées)
    salt: str
    unique_values: int
    skipped: list[str]  # fichiers sans aucune colonne demandée


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
            f"spaCy model `{SPACY_MODEL}` missing — it is a direct-URL inline dependency of the uv "
            "entrypoints (hooks/pii_guard.py, mcp/pc_privacy_server.py), so `uv run --script` "
            "resolves and caches it automatically (large download, ~45 MB, first run only). "
            "Missing here means this module was imported outside such an entrypoint."
        )
    return None


def _build_analyzer():  # type: ignore[no-untyped-def]  # noqa: DAR201
    """Presidio AnalyzerEngine on the French spaCy model + custom SIRET/SIREN/NIR/BIC_SWIFT
    pattern recognizers.

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

    class _LuhnRecognizer(PatternRecognizer):
        """PatternRecognizer whose candidates must also pass the Luhn checksum (defined here to
        keep the `presidio_analyzer` import lazy)."""

        def validate_result(self, pattern_text: str) -> bool | None:
            return _luhn_valid(re.sub(r"\D", "", pattern_text))

    class _BicRecognizer(PatternRecognizer):
        """PatternRecognizer whose candidates must carry an ISO 3166 country code at letters 5-6."""

        def validate_result(self, pattern_text: str) -> bool | None:
            return pattern_text[4:6] in _ISO_COUNTRIES

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "fr", "model_name": SPACY_MODEL}],
        }
    )
    analyzer = AnalyzerEngine(
        nlp_engine=provider.create_engine(), supported_languages=["fr"]
    )
    # supported_language="fr" obligatoire : un recognizer "en" (défaut) n'est jamais interrogé.
    analyzer.registry.add_recognizer(
        _LuhnRecognizer(
            supported_entity="FR_SIRET",
            supported_language="fr",
            patterns=[Pattern("siret", FR_SIRET_PATTERN, 0.6)],
        )
    )
    analyzer.registry.add_recognizer(
        _LuhnRecognizer(
            supported_entity="FR_SIREN",
            supported_language="fr",
            patterns=[Pattern("siren", FR_SIREN_PATTERN, 0.4)],
        )
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="FR_NIR",
            supported_language="fr",
            patterns=[Pattern("nir", FR_NIR_PATTERN, 0.6)],
        )
    )
    analyzer.registry.add_recognizer(
        _BicRecognizer(
            supported_entity="BIC_SWIFT",
            supported_language="fr",
            patterns=[Pattern("bic_swift", BIC_SWIFT_PATTERN, 0.5)],
            # Sans IGNORECASE (défaut de Presidio) : un BIC est en majuscules par spec.
            global_regex_flags=re.DOTALL | re.MULTILINE,
        )
    )
    # CreditCard n'est pas instancié par Presidio hors "en" : on l'enregistre en "fr" (Luhn conservé).
    from presidio_analyzer.predefined_recognizers import CreditCardRecognizer

    analyzer.registry.add_recognizer(CreditCardRecognizer(supported_language="fr"))
    if os.environ.get("PC_PRIVACY_NER", "").strip().lower() == "gliner":
        _add_gliner(analyzer)
    return analyzer


def _add_gliner(analyzer) -> None:  # type: ignore[no-untyped-def]
    """Register a French-language GLiNER recognizer next to spaCy's NER.

    Args:
        analyzer: The Presidio analyzer to register the recognizer on (untyped — Presidio
            ships no stubs).

    Raises:
        AnonymizeError: If GLiNER is not installed or its model cannot load (no silent fallback).
    """
    try:
        from presidio_analyzer.predefined_recognizers import GLiNERRecognizer
    except ImportError as e:
        raise AnonymizeError(
            "PC_PRIVACY_NER=gliner but GLiNER support is missing — install "
            "`presidio-analyzer[gliner]` (add it to the inline `dependencies`)."
        ) from e
    model = os.environ.get("PC_PRIVACY_GLINER_MODEL", GLINER_DEFAULT_MODEL)
    threshold = float(os.environ.get("PC_PRIVACY_GLINER_THRESHOLD", "0.3"))
    try:
        recognizer = GLiNERRecognizer(
            model_name=model,
            entity_mapping=GLINER_ENTITY_MAPPING,
            # Obligatoire : le défaut "en" ne serait jamais interrogé (même cause que plus haut).
            supported_language="fr",
            threshold=threshold,
            flat_ner=False,
            multi_label=True,
        )
    except (
        Exception
    ) as e:  # téléchargement, hors-ligne sans cache, id invalide, torch absent
        raise AnonymizeError(f"GLiNER model `{model}` could not be loaded: {e}") from e
    analyzer.registry.add_recognizer(recognizer)


# ── format detection ─────────────────────────────────────────────────────────


_SKIP_FORMATS = {"config", "code", "binary"}  # jamais scannés : pas des jeux de données
_TEXTLIKE_MIMES = {
    "application/json",
    "application/x-ndjson",
    "application/csv",
    "application/x-empty",
}


def _is_json_object_line(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except json.JSONDecodeError:
        return False


def _looks_like_config(lines: list[str]) -> bool:
    """True if most non-comment, non-blank lines look like `key: value` / `key = value`."""
    body = [
        ln
        for ln in lines
        if ln.strip() and not ln.lstrip().startswith(("#", ";", "//"))
    ]
    if not body:
        return False
    hits = sum(1 for ln in body if _KV_LINE_RE.match(ln))
    return hits >= max(2, (len(body) * 2) // 3)


# Mime valide (`type/subtype`) : BSD `file` imprime « cannot open » sur stdout avec exit 0.
_MIME_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


def _file_mime(path: Path) -> str | None:
    """`file --mime-type -b <path>`, or None if `file` is unavailable or answers a non-mime.

    Never raises: detection degrades to the content checks of :func:`detect_format`.
    """
    if shutil.which("file") is None:
        return None
    try:
        result = subprocess.run(
            ["file", "--mime-type", "-b", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        mime = result.stdout.strip()
        return mime if _MIME_RE.match(mime) else None
    except (subprocess.SubprocessError, OSError):
        return None


def detect_format(path: Path) -> str:
    """Decide how to scan `path`: content first, extension only as a deny-list and a binary hint.

    Args:
        path: The file to classify.

    Returns:
        One of ``"config"`` / ``"code"`` (skip — not a dataset), ``"csv"``, ``"json"``, ``"parquet"``,
        ``"xml"``, ``"xlsx"``, ``"jsonl"``, ``"binary"`` (skip — pdf/image/docx/archive, not a
        dataset), or ``"text"`` (unrecognized tabular shape — e.g. positional/
        fixed-width — scanned line-by-line with no column semantics).

    Raises:
        AnonymizeError: `path` cannot be read at all.
    """
    suffix = path.suffix.lower()
    if suffix in CONFIG_EXTENSIONS:
        return "config"
    if suffix in CODE_EXTENSIONS:
        return "code"
    if suffix == ".parquet":
        return "parquet"

    mime = _file_mime(path)
    # xlsx d'abord : son mime contient « xml ». XML n'est testé que sur des mimes exacts.
    if mime and "spreadsheetml" in mime:
        return "xlsx"
    if mime in {"text/xml", "application/xml"} or (mime or "").endswith("+xml"):
        return "xml"
    # Ni texte ni conteneur géré (pdf, image, docx, archive…) : jamais de binaire à Presidio.
    if (
        mime
        and not mime.startswith("text/")
        and mime not in _TEXTLIKE_MIMES
        and mime not in {"application/zip", "application/octet-stream"}
    ):
        return "binary"

    try:
        with path.open("rb") as f:
            head = f.read(65536)
    except OSError as e:
        raise AnonymizeError(f"{path.name}: cannot read ({e})") from e
    if head[:2] == b"PK":
        # Zip générique : seul un `xl/workbook.xml` en fait un xlsx.
        try:
            with zipfile.ZipFile(path) as z:
                return "xlsx" if "xl/workbook.xml" in z.namelist() else "binary"
        except zipfile.BadZipFile:
            return "binary"
    if b"\x00" in head or head.startswith(b"%PDF"):
        return "binary"

    # Classification sur le début seul : jamais de lecture complète d'un gros fichier.
    text = head.decode("utf-8", errors="ignore")
    truncated = path.stat().st_size > len(head)
    stripped = text.lstrip()

    # Repli XML par le contenu : `file` absent, ou XML sans déclaration vu comme text/plain.
    if stripped.startswith("<?xml") or re.match(r"<[A-Za-z_][\w.:-]*", stripped):
        return "xml"

    head_lines = text.splitlines()
    if truncated and len(head_lines) > 1:
        head_lines = head_lines[:-1]  # dernière ligne possiblement coupée
    nonblank = [ln for ln in head_lines[:30] if ln.strip()]
    if len(nonblank) >= 2 and all(_is_json_object_line(ln) for ln in nonblank):
        return "jsonl"

    # `[{` ou `[]` seulement : une ligne de log `[INFO] …` n'est pas du JSON.
    if re.match(r"\[\s*[{\]]", stripped):
        return "json"
    if stripped.startswith("{"):
        return "config"  # objet JSON unique : config, pas un jeu de données

    sample_lines = head_lines[:30]
    if _looks_like_config(sample_lines):
        return "config"

    try:
        csv.Sniffer().sniff("\n".join(sample_lines[:15]), delimiters=",;\t|")
        return "csv"
    except csv.Error:
        pass

    return "text"


# ── file IO ──────────────────────────────────────────────────────────────────


def _sniff_csv_dialect(path: Path) -> csv.Dialect | type[csv.Dialect]:
    """The delimiter actually used (French exports are often `;`); falls back to `csv.excel`."""
    with path.open(newline="", encoding="utf-8", errors="ignore") as f:
        sample = f.read(4096)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel  # virgule, dernier recours


def _read_rows(path: Path, fmt: str) -> tuple[list[str], list[dict]]:
    """Load a CSV/JSON (array-of-objects)/Parquet file into (fieldnames, rows).

    Args:
        path: The file to read.
        fmt: Format from :func:`detect_format` (xml/xlsx/text never reach this function).

    Returns:
        (fieldnames, rows) — column order and a list of row dicts.

    Raises:
        AnonymizeError: On an unsupported `fmt`, invalid or non-object JSON, or missing pyarrow.
    """
    if fmt == "csv":
        dialect = _sniff_csv_dialect(path)
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, dialect=dialect)
            rows = list(reader)
            return list(reader.fieldnames or []), rows
    if fmt == "json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise AnonymizeError(f"{path.name}: not valid JSON ({e})") from e
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise AnonymizeError(f"{path.name}: expected a JSON array of objects")
        fieldnames = list(dict.fromkeys(k for r in data for k in r))
        return fieldnames, data
    if fmt == "jsonl":
        try:
            rows = [
                json.loads(ln)
                for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
        except json.JSONDecodeError as e:
            raise AnonymizeError(f"{path.name}: invalid JSON line ({e})") from e
        if not all(isinstance(r, dict) for r in rows):
            raise AnonymizeError(f"{path.name}: expected one JSON object per line")
        fieldnames = list(dict.fromkeys(k for r in rows for k in r))
        return fieldnames, rows
    if fmt == "parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as e:
            raise AnonymizeError(
                "Parquet support needs pyarrow (install it into the server env)"
            ) from e
        table = pq.read_table(path)
        return list(table.column_names), table.to_pylist()
    raise AnonymizeError(
        f"{path.name}: unsupported format for row-based reading ({fmt})"
    )


def _write_rows(path: Path, fmt: str, fieldnames: list[str], rows: list[dict]) -> Path:
    out = path.with_suffix(f".anon{path.suffix}")
    if fmt == "csv":
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    elif fmt == "json":
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif fmt == "jsonl":
        out.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
    elif fmt == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq

        pq.write_table(pa.Table.from_pylist(rows), out)
    else:
        raise AnonymizeError(f"{path.name}: cannot write, unsupported format ({fmt})")
    return out


def _xlsx_column_samples(path: Path) -> dict[str, list[str]]:
    """`Sheet!Column` -> up to SAMPLE_SIZE sampled values, across every sheet."""
    try:
        import openpyxl
    except ImportError as e:
        raise AnonymizeError(
            "xlsx support needs openpyxl (pip install openpyxl)"
        ) from e
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # openpyxl lève ses propres erreurs zip/xml
        raise AnonymizeError(f"{path.name}: not a readable xlsx ({e})") from e
    samples: dict[str, list[str]] = {}
    for ws in wb.worksheets:
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = next(rows_iter)
        except StopIteration:
            continue
        headers = [str(h) if h is not None else f"col{i}" for i, h in enumerate(header)]
        for row in rows_iter:
            for h, v in zip(headers, row):
                if v is None or v == "":
                    continue
                key = f"{ws.title}!{h}"
                bucket = samples.setdefault(key, [])
                if len(bucket) < SAMPLE_SIZE:
                    bucket.append(str(v))
    wb.close()
    return samples


def _mask_xlsx(
    path: Path, columns: set[str], pseudonym: Callable[[str, str], str]
) -> tuple[Path, int]:
    """Pseudonymize `Sheet!Column` entries in `columns` across every sheet, write `.anon.xlsx`."""
    try:
        import openpyxl
    except ImportError as e:
        raise AnonymizeError(
            "xlsx support needs openpyxl (pip install openpyxl)"
        ) from e
    try:
        wb = openpyxl.load_workbook(path)
    except Exception as e:
        raise AnonymizeError(f"{path.name}: not a readable xlsx ({e})") from e
    hit: set[str] = set()
    for ws in wb.worksheets:
        rows_iter = ws.iter_rows()
        try:
            header_row = next(rows_iter)
        except StopIteration:
            continue
        headers = [
            str(c.value) if c.value is not None else f"col{i}"
            for i, c in enumerate(header_row)
        ]
        wanted_idx = {
            i: h for i, h in enumerate(headers) if f"{ws.title}!{h}" in columns
        }
        if not wanted_idx:
            continue
        for row in rows_iter:
            for i, h in wanted_idx.items():
                cell = row[i]
                if cell.value not in (None, ""):
                    cell.value = pseudonym(f"{ws.title}!{h}", str(cell.value))
                    hit.add(f"{ws.title}!{h}")
    out = path.with_suffix(".anon.xlsx")
    wb.save(out)
    return out, len(hit)


# Identifiants à format fixe : le match doit couvrir toute la valeur, et la colonne exige cette
# part de valeurs conformes. Les autres entités restent tolérantes (texte libre possible).
WHOLE_VALUE_ENTITIES = frozenset({"BIC_SWIFT", "FR_SIREN", "FR_SIRET"})
WHOLE_VALUE_MIN_RATIO = 0.8


# Format produit par `_make_pseudonymizer` : `COLUMN_` + 8 hexa.
_PSEUDONYM_RE = re.compile(r"[A-Z][A-Z0-9_]*_[0-9a-f]{8}")


def _maskable_results(results, text: str) -> list:
    """The analyzer results that count as PII, once implausible ones are dropped:

    - a `PHONE_NUMBER` overlapping a higher-scored `DATE_TIME` (only that pair, so a real phone
      number is kept);
    - a `PERSON` containing an underscore (an identifier, not a name);
    - a `PHONE_NUMBER` with fewer than 8 digits.

    Args:
        results: Presidio ``RecognizerResult`` list for `text`.
        text: The exact string that was analyzed (spans index into it).

    Returns:
        The results whose entity type is in `DEFAULT_MASK_ENTITIES` and that pass both rules.
    """
    dates = [r for r in results if r.entity_type == "DATE_TIME"]
    return [
        r
        for r in results
        if r.entity_type in DEFAULT_MASK_ENTITIES
        and not (
            r.entity_type == "PHONE_NUMBER"
            and any(
                d.score > r.score and d.start < r.end and r.start < d.end for d in dates
            )
        )
        and not (r.entity_type == "PERSON" and "_" in text[r.start : r.end])
        and not (
            r.entity_type == "PHONE_NUMBER"
            and sum(c.isdigit() for c in text[r.start : r.end]) < 8
        )
    ]


# Scan ligne à ligne : pas de vote par colonne, donc seules les entités vérifiables par leur
# format alertent (PERSON exclu : trop de faux positifs en prose).
_STRICT_TEXT_ENTITIES = DEFAULT_MASK_ENTITIES - {"PERSON"}


def _text_line_findings(
    path: Path,
    analyzer,
    max_lines: int | None = None,
    truncated: Callable[[str], None] | None = None,
) -> list[Finding]:
    """Scan every non-empty line of an unrecognized tabular file — no sampling, no columns.

    Only format-checkable entities are reported (see `_STRICT_TEXT_ENTITIES`); masking in
    :func:`_mask_text` still covers `PERSON`.

    Args:
        path: The file to scan.
        analyzer: A built Presidio ``AnalyzerEngine``.
        max_lines: Scan at most this many lines (about 3 ms each); None scans them all.
        truncated: Called with the file name when `max_lines` cut the scan short.

    Returns:
        One Finding per entity type found on the scanned lines.

    Raises:
        AnonymizeError: The file cannot be read.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as e:
        raise AnonymizeError(f"{path.name}: cannot read ({e})") from e
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        if truncated is not None:
            truncated(path.name)
    hits: Counter[str] = Counter()
    for line in lines:
        if not line.strip():
            continue
        for result in _maskable_results(
            analyzer.analyze(text=line, language="fr"), line
        ):
            if result.entity_type in _STRICT_TEXT_ENTITIES:
                hits[result.entity_type] += 1
    return [
        Finding(path.name, f"(no columns — {entity})", f"detected on {count} line(s)")
        for entity, count in hits.items()
    ]


def _mask_text(path: Path, analyzer) -> tuple[Path, int]:
    """Mask every DEFAULT_MASK_ENTITIES span, line by line, with a same-length filler.

    No column confirmation and no salt: every matched span is masked automatically. Same-length
    substitution keeps a fixed-width file's column boundaries valid. The file is written as UTF-8,
    so byte offsets shift on non-ASCII lines of a file in another encoding.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines(
            keepends=True
        )
    except OSError as e:
        raise AnonymizeError(f"{path.name}: cannot read ({e})") from e
    hit = 0
    out_lines: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        results = sorted(
            (
                r
                for r in _maskable_results(
                    analyzer.analyze(text=body, language="fr"), body
                )
            ),
            key=lambda r: r.start,
        )
        pieces: list[str] = []
        cursor = 0
        for r in results:
            if r.start < cursor:
                continue  # span chevauchant : on garde le premier
            pieces.append(body[cursor : r.start])
            length = r.end - r.start
            filler = ((r.entity_type + "#") * (length // (len(r.entity_type) + 1) + 1))[
                :length
            ]
            pieces.append(filler)
            cursor = r.end
            hit += 1
        pieces.append(body[cursor:])
        out_lines.append("".join(pieces) + eol)
    out = path.with_suffix(f".anon{path.suffix}" if path.suffix else ".anon.txt")
    out.write_text("".join(out_lines), encoding="utf-8")
    return out, hit


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


def _column_samples(path: Path, fmt: str) -> dict[str, list[str]]:
    """Column/tag name -> up to SAMPLE_SIZE non-empty sampled values, for a columnar format."""
    if fmt == "xml":
        return _xml_leaf_samples(path.read_text(encoding="utf-8", errors="ignore"))
    if fmt == "xlsx":
        return _xlsx_column_samples(path)
    fieldnames, rows = _read_rows(path, fmt)
    return {
        col: [str(r[col]) for r in rows if r.get(col)][:SAMPLE_SIZE]
        for col in fieldnames
    }


def detect_findings(
    files: list[str],
    values_only: bool = False,
    text_max_lines: int | None = None,
    truncated: Callable[[str], None] | None = None,
) -> list[Finding]:
    """Run Presidio + the name heuristic over sampled values of every column/tag in `files`.

    Format is decided per file by :func:`detect_format`; config, code and binary files are
    silently skipped, and an unrecognized tabular shape is scanned line by line.

    Args:
        files: Paths to any file (format auto-detected).
        values_only: For an anonymized copy (names kept, values replaced): skip the column-name
            heuristic and values already shaped like our pseudonyms.
        text_max_lines: Line budget for a file scanned line by line (None: no limit).
        truncated: Called with the file name when that budget cut a scan short.

    Returns:
        One Finding per column/tag/line judged sensitive, across all files.

    Raises:
        AnonymizeError: If Presidio/the spaCy model is missing, or a file cannot be read at all.
    """
    analyzer = None  # construit au premier besoin (spaCy chargé seulement si utile)
    findings: list[Finding] = []
    for fp in files:
        path = Path(fp)
        fmt = detect_format(path)
        if fmt in _SKIP_FORMATS:
            continue
        if analyzer is None:
            analyzer = _build_analyzer()
        if fmt == "text":
            findings.extend(
                _text_line_findings(path, analyzer, text_max_lines, truncated)
            )
            continue
        for name, values in _column_samples(path, fmt).items():
            if values_only:
                # Nos pseudonymes : le modèle NLP en prend ~1 sur 10 pour un nom.
                values = [v for v in values if not _PSEUDONYM_RE.fullmatch(v)]
            if not values:
                continue
            name_hit = not values_only and any(
                k in name.lower() for k in NAME_HEURISTIC_KEYWORDS
            )
            # Chaque valeur vote une fois par type d'entité masquable ; la colonne est jugée sur le
            # nombre de valeurs portant une entité, pas sur le type dominant (colonnes mixtes).
            votes: Counter[str] = Counter()
            whole_votes: Counter[str] = Counter()
            hit_values = 0
            whole_hit_values = 0
            for v in values:
                # Seules les entités masquables comptent (LOCATION/URL doublonnent EMAIL/IBAN).
                results = _maskable_results(analyzer.analyze(text=v, language="fr"), v)
                found = {
                    r.entity_type
                    for r in results
                    if r.entity_type not in WHOLE_VALUE_ENTITIES
                }
                whole = {
                    r.entity_type
                    for r in results
                    if r.entity_type in WHOLE_VALUE_ENTITIES
                    and not v[: r.start].strip()
                    and not v[r.end :].strip()
                }
                french_id = whole & {"FR_SIRET", "FR_SIREN"}
                if french_id:
                    # Un SIRET Luhn-valide passe aussi pour une carte : l'entité française gagne,
                    # et la valeur compte encore pour le seuil souple (sous son propre type).
                    found.discard("CREDIT_CARD")
                    found |= french_id
                if found:
                    hit_values += 1
                if whole:
                    whole_hit_values += 1
                votes.update(found)
                whole_votes.update(whole)
            min_hits = max(2, len(values) // 3)
            min_whole = max(2, math.ceil(WHOLE_VALUE_MIN_RATIO * len(values)))
            lenient_ok = hit_values >= min_hits
            # SIRET et SIREN peuvent partager une colonne : ratio sur les valeurs, pas par entité.
            whole_ok = whole_votes if whole_hit_values >= min_whole else Counter()
            is_pii = lenient_ok or bool(whole_ok)
            qualified: Counter[str] = Counter(whole_ok)
            if lenient_ok:
                qualified.update(votes)
            ranked = [e for e, _ in qualified.most_common()]
            top_entity = ranked[0] if ranked else ""
            count = hit_values if lenient_ok else qualified[top_entity]
            others = f"; also {', '.join(ranked[1:])}" if len(ranked) > 1 else ""
            if is_pii:
                findings.append(
                    Finding(
                        path.name,
                        name,
                        f"detected {top_entity} ({count}/{len(values)}{others})",
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
        files: Paths to CSV, JSON, JSONL, Parquet, XML, XLSX or line-scanned text files. Config,
            code and binary files are skipped and get no copy.
        columns: Column names (CSV/JSON/JSONL/Parquet), `Sheet!Column` (XLSX) or tag local
            names (XML) to mask. Ignored for a file scanned line by line.
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
    analyzer = None  # construit à la demande (chemin texte seulement)

    for fp in files:
        path = Path(fp)
        fmt = detect_format(path)

        if fmt in _SKIP_FORMATS:
            skipped.append(path.name)
            continue

        if fmt == "xml":
            out, n_hit = _mask_xml(path, wanted, pseudonym)
            if not n_hit:
                skipped.append(path.name)
            outputs.append((path.name, out.name, n_hit))
            continue

        if fmt == "xlsx":
            out, n_hit = _mask_xlsx(path, wanted, pseudonym)
            if not n_hit:
                skipped.append(path.name)
            outputs.append((path.name, out.name, n_hit))
            continue

        if fmt == "text":
            # Pas de colonnes à confirmer : toute entité détectée est masquée (cf. _mask_text).
            if analyzer is None:
                analyzer = _build_analyzer()
            out, n_hit = _mask_text(path, analyzer)
            if not n_hit:
                skipped.append(path.name)
            outputs.append((path.name, out.name, n_hit))
            continue

        fieldnames, rows = _read_rows(path, fmt)
        hit_columns = [c for c in fieldnames if c in wanted]
        if not hit_columns:
            skipped.append(path.name)
        for row in rows:
            for col in hit_columns:
                if row.get(col):
                    row[col] = pseudonym(col, str(row[col]))
        out = _write_rows(path, fmt, fieldnames, rows)
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
        # n == 0 ⇔ rien de masqué ; flag par ligne, donc deux entrées de même nom ne se mélangent pas.
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
