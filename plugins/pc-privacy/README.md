# pc-privacy

Deterministic, coherent PII pseudonymization for Claude Code — an MCP server, no LLM anywhere in
the plugin.

## Overview

Preparing a real dataset for a bug report, a ticket, or a shared brief means masking the personal
data in it *consistently* — the same customer must get the same pseudonym in every file, or the
joins that make the data useful for debugging break. LLMs are unreliable at exhaustive, consistent
masking; this does it deterministically with Presidio's French NLP engine and a blake2b keyed
pseudonym.

Two phases, always in this order:

1. **`anonymize_detect`** — samples each column/tag, runs Presidio + a column-name heuristic,
   returns a table of columns judged sensitive with the entity type and vote count as the reason.
   Never returns a sampled value. Writes nothing.
1. **`anonymize_apply`** — given a human-confirmed column list, writes `<name>.anon.<ext>` next to
   each input. The same real value maps to the same pseudonym across every file in one call, and
   across calls that reuse the returned `salt`. Returns output filenames + the salt — never the
   anonymized content.

Masked by default: `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`,
`FR_SIRET`, `FR_SIREN`. `DATE_TIME` and generic `LOCATION` are left alone — they usually carry the
debugging signal itself (order dates, shipping city).

Formats: CSV, JSON (array of objects), Parquet (lazy `pyarrow`), XML (leaf-tag local names, regex —
no parser, so undeclared-namespace-prefix exports still work).

## First-time setup

The server resolves `mcp` + Presidio automatically via `uv run --script`. Presidio's French spaCy
model is not a pip dependency — run once, ~45 MB:

```
uv run --with spacy python -m spacy download fr_core_news_md
```

Until it is present, `anonymize_detect` returns a `BLOCKED` status with this exact command.

## Tools

### `anonymize_detect(files)`

Report PII-looking columns/tags across `files`. Writes nothing. Run first, always.

### `anonymize_apply(files, columns, salt=None)`

Write `<name>.anon.<ext>` for each file, pseudonymizing `columns` (column names for
CSV/JSON/Parquet, tag local names for XML). Pass `salt` from an earlier call to keep the two
batches joinable; omit it for a fresh random salt, returned in the result.

## Requirements

`uv` on `PATH` (as for any plugin shipping an `mcp/` server), plus the one-time spaCy model
download above.
