---
name: data-schema
description: Read a data file — .xlsx, .csv or .parquet that holds rows rather than prose — as its sheets, columns with their types and at most 5 sample rows per sheet, instead of every cell. Use whenever the user points at such a file and you need to know what is in it.
argument-hint: "<path-to-data-file>"
allowed-tools: Bash(uv run --script:*), Read
---

# Read a data file by its schema, not its cells

A workbook weighs by its cell count, not by its format: converting it to Markdown or CSV keeps every
cell. What reads usefully is the sheets, the columns and their types, a few rows, then targeted
queries (DuckDB).

## Run

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/data_schema.py <file>
```

The output is Markdown: per sheet, a `column | type` table, then at most 5 rows. Use it to write a
precise query instead of reading the file.

## Personal data

The sample does not go through `Read`, so the `pc-privacy` guard never sees it; the script asks that
guard itself first. When it flags the file, or fails, or times out, the output is the schema alone and
says **Rows withheld**: do not try to get the rows another way (`Read`, a dump, a conversion) — tell the
user and let them decide. When `pc-privacy` is not installed, the sample is shown.

## Limits

Types come from the first 100 non-empty rows (a `.parquet` states its own). Row counts are not
reported. A `.xlsx` of prose (specs, a data dictionary) is for `document-to-md`.
