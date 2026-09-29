# pc-privacy

Deterministic, coherent PII pseudonymization for Claude Code — an MCP server and a `Read` guard,
no LLM anywhere in the plugin.

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

A column is flagged when at least one masked entity type shows up in enough of its sampled values;
a mixed column (some emails, some phones, some names) counts by the sum of its votes, not by its
single most frequent type. `FR_SIRET`/`FR_SIREN` are Luhn-validated, so an arbitrary 9-digit
internal id is not flagged. La Poste identifiers do not follow Luhn and are missed. `BIC_SWIFT` requires an ISO 3166 country code at letters 5-6, which
rules out most all-caps words (`APPROVED`) but not those that happen to carry one (`TRANSACTION`).

A `PERSON` guess whose text contains an underscore (`COST_CENTER`) is dropped: an identifier, not a
name. A `PHONE_NUMBER` guess overlapping a higher-scored date (`2021-10-01-07:00`) is dropped too.

Fixed-format identifiers (`BIC_SWIFT`, `FR_SIREN`, `FR_SIRET`) are judged like a data-profiling
rule: a value conforms only if the match covers the **whole** value, and the column is flagged when
at least 80 % of its sampled values conform (`WHOLE_VALUE_MIN_RATIO`). A word inside a sentence
(`Business Line : TRANSACTION FRANCE`) is not a BIC. Emails, IBAN, phones, cards and NIR keep the
lenient one-third vote with partial matches, since they can sit inside a free-text comment.

## Formats

The format is decided from the file's content (`file --mime-type`, binary signature, head of the
file), not from its extension.

| Format | Handling |
|---|---|
| CSV | Column-based. Delimiter sniffed (`,` `;` tab `\|`) |
| JSON (array of objects), JSONL | Column-based, keys are the columns |
| XLSX | Every sheet, columns named `Sheet!Column` |
| Parquet | Column-based, lazy `pyarrow` |
| XML | Leaf-tag local names, regex — no parser, so undeclared-namespace exports still work |
| Anything else that is text (logs, positional/fixed-width exports, one-column lists) | Scanned line by line, no column semantics — see below |
| Config (`.yml .yaml .toml .ini .cfg .conf .env .properties`, a single JSON object, `key: value` text), code and docs (`.py .js .ts .sh .md ...`), binaries (pdf, images, docx, pptx) | Never scanned — not datasets. `.sql` is scanned: a data dump carries real PII |

**Line-by-line files.** With no columns to confirm, `anonymize_apply` ignores `columns` for such a
file and masks every default-masked entity it finds, line by line. Line length is preserved so a
fixed-width layout survives; the file is rewritten as UTF-8. `anonymize_detect` reports these as
`(no columns — <ENTITY>)`.

## Read guard (hook)

`hooks/pii_guard.py` runs on every `Read` (`PreToolUse`). It classifies the file from its head;
config, code and binaries pass with no output and never load the NLP model. For anything else it
runs the same detection as `anonymize_detect`.

- The decision is always `ask`, never `deny`: the person sees which columns look sensitive
  (names and entity types, never values) and chooses to read as-is, or to refuse and run
  `anonymize_apply` then read the `.anon` file.
- **Fail-open.** Any internal error lets the `Read` through with a line on stderr — a guard's own
  bug never blocks a tool call. Run `uv run --script hooks/pii_guard.py --check` after install or
  a dependency change: it exits 0 only if Presidio and the spaCy model load.
- A file whose name contains `.anon.` (the `anonymize_apply` output) is judged on its values only:
  it keeps its column/tag names (`IBAN`, `First_Name`), so the name heuristic would flag every copy.
  Values already shaped like the plugin's own pseudonyms (`LAST_NAME_a48ea4d9`) are ignored too,
  since the NLP model takes roughly one in ten for a person's name. A real value left in it is
  still caught.
- Files above `PII_GUARD_MAX_MB` (default 100) are not scanned; the `ask` says the file was NOT
  analyzed.
- Cost: about 2 s per `Read` of a dataset, mostly the spaCy model load. Each scan is
  independent; verdicts are not cached.
- **`@file` mentions.** Claude Code inlines an `@path` itself, never through the `Read` tool, so
  no `PreToolUse` hook sees it. The same script is also registered on `UserPromptSubmit`, which
  receives the prompt with the literal `@path`: if a mentioned file would raise an alert, the
  message is **blocked** (a prompt hook cannot `ask`) with the same column/entity report. Mention
  the path without the `@` to get the usual `ask` dialog, attach the `.anon` copy, or type
  `pii:ok` anywhere in the message to send it anyway (it also lifts the block for a message that
  merely quotes an `@path`). Only the person can type it — the model does not write the prompt. A file
  attached through a UI upload rather than typed as `@path` is still out of reach.
- Alongside `read_cost.py` (also `ask` on `Read` for heavy formats) both hooks answer
  independently on the same event; neither overrides the other.

## Optional: GLiNER name detection

`fr_core_news_md` misses ALL-CAPS names (`JEAN DUPONT` comes back as `LOCATION`, not `PERSON`).
Setting `PC_PRIVACY_NER=gliner` adds a GLiNER recognizer next to spaCy — never instead of it — and
catches them, but it also reads short codes and numbers as entities. On a Workday change-job export
it flagged 8 more tags than spaCy alone (`Contract_Type`, `Job_Profile`, `Pay_Cycle_Hours`,
`Order`…), none of them personal data, and lost none. It also makes `detect` ~11× slower, adds ~4 s
to the analyzer build, and downloads several hundred MB on first use. Off by default; turn it on
only for a dataset that carries capitalised names under neutral column names. The hook and the MCP
server do not bundle it: enabling it means adding `presidio-analyzer[gliner]` to their inline
dependencies (the hook otherwise fails open with a stderr line).

| Variable | Effect |
|---|---|
| `PC_PRIVACY_NER=gliner` | Enable the GLiNER recognizer |
| `PC_PRIVACY_GLINER_MODEL` | Override the model id |
| `PC_PRIVACY_GLINER_THRESHOLD` | Override the confidence threshold |

`evals/gliner_compare.py` (`uv run --script evals/gliner_compare.py FILE...`) prints, per file, the
columns flagged only with or only without GLiNER and both timings — never a value. `evals/` is
gitignored, so the script exists only on the machine that has it.

## Setup

Nothing to install beyond `uv` on `PATH`: the MCP server and the hook are `uv run --script`
entrypoints whose inline metadata pulls Presidio, `openpyxl` and the French spaCy model
(`fr-core-news-md`, ~45 MB, cached on first run). If `anonymize_detect` returns a `BLOCKED` status
saying the model is missing, `anonymize.py` was imported outside one of those entrypoints.

## Tools

### `anonymize_detect(files)`

Report PII-looking columns/tags across `files`. Writes nothing. Run first, always. Config, code and
binary files are skipped silently.

### `anonymize_apply(files, columns, salt=None)`

Write `<name>.anon.<ext>` for each file, pseudonymizing `columns` (column names for
CSV/JSON/JSONL/Parquet, `Sheet!Column` for XLSX, tag local names for XML). Files scanned line by
line ignore `columns` and are masked entity by entity. Config, code and binary files are listed in
`skipped` and get no `.anon` copy; an xml/xlsx/line-by-line file where nothing matched is also
listed in `skipped` but still gets one. Pass `salt` from an earlier call to keep two batches joinable; omit it for a
fresh random salt, returned in the result.

## Requirements

`uv` on `PATH` (as for any plugin shipping an `mcp/` server). The `file` command improves format
detection and is present by default on Linux and macOS; without it (native Windows) detection
falls back to the file's content and extension alone; that path is covered by a mock only, and a
zip container (docx, zip) is then tried as xlsx and errors — the hook fails open on it.
