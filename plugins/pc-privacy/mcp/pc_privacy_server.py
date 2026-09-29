#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp", "presidio-analyzer", "presidio-anonymizer", "openpyxl", "fr-core-news-md @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl"]
# ///
"""pc-privacy MCP server — deterministic PII pseudonymization as native tools (`anonymize_detect`,
`anonymize_apply`).

Thin wrapper around `anonymize.py`'s functions — no reasoning here, no LLM anywhere in this plugin.
Neither tool returns any anonymized content: `detect` returns a column report (entity types + vote
counts, never a sampled value), `apply` returns output filenames + the salt. The human reads the
`<name>.anon.<ext>` files themselves.

Declared via `.mcp.json` at the plugin root. `uv run --script` resolves `mcp` + Presidio from the
inline metadata above — no pip-install step for the plugin installer beyond having `uv`.

The French spaCy model (`fr-core-news-md`) is a direct-URL inline dependency above: `uv run
--script` resolves and caches it on the first run (~45 MB) — no manual download step. If it is
ever reported missing, this server was started outside that entrypoint, and `anonymize_detect`
returns a BLOCKED status saying so.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anonymize
from mcp.server import MCPServer

m = MCPServer("pc-privacy")


@m.tool()
def anonymize_detect(files: list[str]) -> str:
    """Report which columns/tags of local data files look like PII — writes nothing.

    Run this FIRST on any dataset before it goes into a brief or a ticket. Returns a table of
    columns judged sensitive (by Presidio's French NLP + a column-name heuristic), with the entity
    type and vote count as the reason — never a sampled value. Confirm/correct the column list with
    a human, then pass those names to `anonymize_apply`. Format is detected from content (not the
    extension): CSV (`,` `;` tab `|`), JSON (array of objects), JSONL, Parquet, XLSX (every sheet,
    columns named `Sheet!Column`), XML (leaf-tag local names). Config-shaped files and binaries are
    skipped. An unrecognized tabular shape (e.g. fixed-width) is scanned line by line instead.
    """
    try:
        findings = anonymize.detect_findings(files)
    except anonymize.AnonymizeError as e:
        return f"[anonymize_detect] BLOCKED — {e}"
    return anonymize.format_detect_report(findings)


@m.tool()
def anonymize_apply(
    files: list[str], columns: list[str], salt: str | None = None
) -> str:
    """Write an anonymized copy (`<name>.anon.<ext>`) of each file, pseudonymizing `columns`.

    Pseudonyms are coherent: the same real value maps to the same pseudonym across every file in
    this call (joins survive), and across calls that pass the same `salt`. Pass the `salt` from a
    previous call's output to keep a later batch joinable with it; omit it for a fresh random salt,
    which is returned in the result. `columns` are column names for CSV/JSON/Parquet, tag local
    names for XML, `Sheet!Column` for XLSX. A file with no columns (a log, a fixed-width export) is
    masked entity by entity automatically and `columns` does not apply to it. Config, code and binary
    files are skipped. Returns only output filenames + counts + the salt — never the anonymized
    content.
    """
    try:
        outcome = anonymize.apply_anonymization(files, columns, salt)
    except anonymize.AnonymizeError as e:
        return f"[anonymize_apply] BLOCKED — {e}"
    return anonymize.format_apply_report(outcome)


if __name__ == "__main__":
    m.run()
