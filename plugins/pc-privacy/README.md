# pc-privacy

Deterministic, coherent PII pseudonymization for Claude Code — an MCP server and a `Read` guard,
with an optional local decision-model gate on `Read`.

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
name. A `PHONE_NUMBER` guess overlapping a higher-scored date (`2021-10-01-07:00`), or with fewer than 8
digits (a bare `1640`), is dropped too.

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
- A file scanned line by line (a log, a positional export) costs ~3 ms a line, so only its first
  `PII_GUARD_MAX_TEXT_LINES` lines (default 3000) are read; past that the `ask` says the rest was
  NOT fully analyzed, even when the lines read are clean.
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

### Read band

For a `Read` the guard flags, a mod (`hooks/pii-hold/register.tsx`, written for the Claude Code
mods API, early access, measured on 2.1.288) holds the call and draws a framed band above the
prompt: `⚠︎ Personal data`, the file name and the sensitive columns, then one button per outcome (the first press wins):

- `⎘ anonymize` (`1`) writes the anonymized copy (`pii_guard.py --anonymize`, the same detection and
  masking as `anonymize_detect` then `anonymize_apply`) and reads that copy instead of the original;
  Claude is told it reads a copy. When no copy can be made the read is refused, never silently
  redirected to the original. Not offered for an oversize file or for a file that is already a
  `.anon` copy.
- `✔︎ allow read` (`2`) reads the file as it is, with no second dialog — the guard's `ask` is lifted
  for that exact call and path only.
- `✘ deny` (`0`) refuses the `Read`; Claude asks what to do and can propose `anonymize_apply`.

The guard stays the source of the verdict: the mod asks `pii_guard.py --verdict` (the analysis
loads cold, so the call gets 55 s) and never judges by itself. When nobody answers within one
minute, when the verdict cannot be read, when a settings rule or the engine decides the read, or
when the session draws on neither a terminal nor the desktop app (`claude -p`, the VS Code
extension alone), nothing is held and the guard's `ask` applies. A mention `@file` in a prompt is
not held: its block stays.

The mod is TypeScript, which the repo's gates do not run. Check it with
`claude plugin validate plugins/pc-privacy`, `claude plugin test plugins/pc-privacy` and
`tsc -p plugins/pc-privacy`.

## Optional: decision-model gate

With a decision model served locally, the Read guard first asks it one question about a bounded
digest of the file: personal data as the guard's rules define it, yes or no. A probability below 0.5 (a model "pass") ends the guard
there: Presidio does not run and the file goes through; 0.5 or more still runs Presidio for the column listing and adds
one line saying the model also judged the file personal. A file whose name contains `.anon.` never
goes to the model.

| Variable | Effect |
|---|---|
| `ONE_DECISION_MODEL` | The mlx-serve model id. Unset: the gate is off |
| `ONE_DECISION_URL` | Server address, default `http://127.0.0.1:11234`. Only `127.0.0.1`, `localhost` and `::1` are accepted |

- **Silent fallback.** Variable unset, server unreachable, any HTTP error, or a digest the server
  refuses because it does not fit its context window: the gate is skipped with no message and
  Presidio decides alone, exactly as without the gate. The server is probed with a short TCP
  connection before the digest is built, and after a connection failure or a timeout the gate
  stays off for the rest of that hook run, so a down server adds neither a second parse of a
  large file nor a timeout per `@file`.
- **You start the server.** The hook starts no process. Run `mlx-serve` yourself and keep the model
  loaded: a cold start costs seconds on every `Read`.
- **Values stay local.** The digest sent to the model holds the format, the column names and up to
  three sampled values per column, each cut at 60 characters, never the file name. A text file
  sends its first 10 lines, cut the same way. Newlines and other control characters in the content
  become spaces. It goes to a loopback address only.
- **Bind loopback.** `mlx-serve` listens on all interfaces by default: start it with
  `--host 127.0.0.1` so the model is not reachable from the network.
- **Bounded digest.** The digest lists every column, never a sample of columns, so a wide file can
  exceed the server's window and fall back to Presidio.
- **A pass stops the guard.** A text file longer than `PII_GUARD_MAX_TEXT_LINES` lines that the
  model passes is not scanned further, and the notice about lines not analyzed is not emitted.

`evals/decision_compare.py` (`uv run --script evals/decision_compare.py FILE[:block|:pass]...`)
prints, per file, the Presidio-only decision, the gate's probability and the decision with the gate
active, then the misses and false alarms of each. It needs the running server and prints no value.

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
columns flagged only with or only without GLiNER and both timings — never a value. Eval sources
(`evals/*.py`) are tracked; results written next to them are gitignored.

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
falls back to the file's content and extension alone; that path is covered by a mock only. A zip
container counts as an xlsx only if it holds `xl/workbook.xml`, otherwise it is skipped as binary.
