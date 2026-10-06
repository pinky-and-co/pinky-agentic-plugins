---
name: debug-brief
argument-hint: "<behavior discrepancy> --code <src…> --data <file…> [--spec <path>] [--columns c1,c2] [--salt s] [--send [model]]"
description: "Assemble a self-contained debug brief — a human-reported behavior discrepancy confronted against the code, an optional spec, and a coherently anonymized sample of the real data — so a model can reason about root cause without ever seeing real customer/business data. Output is one text block to paste into an external model, or send through the panel MCP tool. Use when someone reports 'X does the wrong thing' and wants a rigorous bug/not-a-bug qualification before touching the code."
user-invocable: true
allowed-tools: Read, Bash(find:*), Bash(rtk find:*), Bash(ls:*), Bash(rtk ls:*), Skill
metadata:
  source: original
  updated: 2026-08-28
---

# Debug Brief

Assemble a self-contained debug brief. The brief confronts a **human-reported behavior discrepancy**
against the code, an optional spec, and a **coherently anonymized** sample of the dataset the bug
was reported against — so a model can reason about root cause without ever seeing real
customer/business data. The output is a single text block: no commentary, no preamble.

The point of the skill is the **qualify-first discipline** — is this actually a defect, or
intended/documented behavior the reporter isn't aware of? — and the **hard data boundary**: real
values are pseudonymized by a deterministic tool *before* any model sees them.

## Preflight

1. **`pc-privacy` reachable?** This skill masks the dataset through the `anonymize_detect` /
   `anonymize_apply` MCP tools (`pc-privacy` server) — a declared `dependencies` entry of `pc-craft`,
   so a normal install resolves it. If the tools are not in your tool list, the dependency did not
   resolve: stop and tell the user to reinstall `pc-craft` (or install `pc-privacy` directly) and
   run its one-time spaCy model download. Do **not** fall back to reading the raw file or to a
   hand-rolled masking pass.
2. **`--send` requested but `panel` unreachable or empty?** The `panel` MCP tool (`pc-core-models`
   server) runs the brief through one non-Claude pool model (sonnet/opus-tier entries only). If
   `--send` was passed and `panel` is missing from your tool list, or it returns `BLOCKED` (no
   eligible pool entry), produce the brief block instead and say the send step was skipped.

## Hard invariant — never read the raw dataset

**You (the assembling agent) must never `Read()` a raw `--data` file.** Only the `anonymize_*` MCP
tools — deterministic, no LLM — touch real values. You read only the generated `*.anon.*` output.
This is the whole point: anonymization happens strictly *before* any model (local or cloud) sees the
data, coherently (the same real value produces the same pseudonym everywhere it appears, across
every file passed to one `anonymize_apply` call — preserving joins for debugging) and
deterministically (no inconsistent masking an LLM rewrite could introduce).

## Arguments

`$ARGUMENTS` — a human description of the unexpected behavior (inline text, or a path to a file
containing it), plus:

| Flag | Description |
|---|---|
| `--code <path...>` | One or more source files implicated in the bug (required) |
| `--spec <path>` | Optional spec / ADR / ticket describing the *expected* behavior |
| `--data <path...>` | One or more CSV/JSON/Parquet/XML files to anonymize and include (required) |
| `--columns <col1,col2,...>` | Pre-confirmed PII columns to mask — skips the detect/confirm loop (the flag *is* the confirmation) |
| `--salt <salt>` | Pin the anonymization salt — reuse across sessions on the same dataset to keep pseudonyms stable and joinable |
| `--send [model]` | After assembling, run the brief through the `panel` MCP tool (one non-Claude pool model) instead of only outputting it. `[model]` optionally names a pool entry |

**Known gap**: `--data` accepts local files only, not a live Snowflake table/query. If the data
lives in Snowflake, extract it to CSV/Parquet first (Snowsight export, or `SELECT … ; !spool` in
SnowSQL), then pass the extract — do not bootstrap a data session from this skill.

Resolve inputs in order:

1. Explicit values in `$ARGUMENTS` / flags.
1. Ask for whichever of (description, `--code`, `--data`) is missing — never guess or proceed on
   silence for any of the three. `--spec` stays optional.

## Step 1 — Read context

1. Read the repo's `pyproject.toml` `[project].description` (the mission) if present, else the
   README tagline.
1. Read each `--code` file in full.
1. Read `--spec` in full if given. If its extension is `.docx` / `.pptx` / `.xlsx` / `.pdf` /
   `.html` / `.htm`, invoke the `pc-core:document-to-md` skill on it first and read that Markdown
   instead — the assembled brief is a static block pasted into an external model, which never sees
   this conversion rule.

## Step 2 — Detect PII columns

Call the `anonymize_detect` MCP tool (`pc-privacy` server) on the `--data` files — never read them
yourself.

- **`--columns` given**: show the detect report as an FYI audit trail, then go straight to Step 3
  with the given columns.
- **`--columns` not given**: show the detect report to the user and ask *"Confirm this column list
  for masking, or provide corrections?"* Wait for an explicit answer. If detect flags nothing,
  **still ask** — an empty report does not mean the dataset is clean; say so.

## Step 3 — Anonymize

Call the `anonymize_apply` MCP tool with the confirmed column list (and `--salt` if given). It
writes `<name>.anon.<ext>` next to each original and returns the salt used — record that salt in the
brief's DATA section so a later run on the same dataset stays joinable. Read **only** the
`*.anon.*` files from here on.

## Step 4 — Deliver

**If `--send [model]` is present:** call the `panel` MCP tool with a single task —
`tasks=["Follow the ROLE / GUIDELINES / OUTPUT FORMAT instructions embedded in the brief below.\n\n"
+ <the full brief block>]` — and `models=["<model>"]` if `[model]` names a pool entry, else omit
`models`. The brief goes inline in the task; there is no file parameter. Show the answer verbatim,
prefixed with which model produced it (the tool's header reports this). The model has not been
scored against any oracle — tell the user to verify the QUALIFICATION independently before trusting
the SUGGESTED FIX.

**Otherwise:** output **only** the brief block below. Replace placeholders. No intro text.

---

````
## HUMAN REPORT

<the bug description, verbatim>

---

## MISSION

<pyproject.toml `[project].description`, or a one-sentence repo description if absent>

---

## SPEC (expected behavior)

<--spec file content, or omit this section entirely if not provided>

---

## CODE

<for each --code file: a filename header, then the full file content>

---

## DATA (anonymized sample)

<for each *.anon.* file: a filename header, then full content — if a file exceeds ~200 rows, include
the first 100 + a note "(truncated — N rows total, ask for a specific range if needed)">

Anonymized columns: <list, from Step 2/3>. Salt: <from Step 3>. Untouched columns carry the actual
debugging signal (dates, amounts, statuses, non-identifying IDs) — treat them as ground truth.

---

## ROLE

You are a senior engineer debugging a reported behavior discrepancy. You have the code, optionally a
spec of the *intended* behavior, and an anonymized sample of the real data the bug was reported
against. Reason from evidence in the data + code — do not invent behavior neither shows. **Qualify
first**: is this actually a defect, or intended/documented behavior the reporter isn't aware of?

---

## GUIDELINES

- **Do not assume it's a bug.** Qualify before explaining — a well-reasoned wrong verdict is worse
  than an admitted "insufficient information," and harder to catch on review.
- **Ground every claim in a specific line, tag, or row.** A claim with no citation is a guess —
  label it as such.
- **Distinguish confirmed from hypothesis.** If the material doesn't prove the mechanism, say what
  additional data or logs would confirm or rule it out — do not fill the gap with invented behavior.
- **The anonymized values are opaque tokens, not noise.** Two rows sharing the same `EMAIL_xxxx`
  token are the same real customer — use that for cross-row reasoning exactly as you would the real
  value.
- Do not comment on code style, formatting, or naming unless it is the mechanism of the discrepancy.

---

## OUTPUT FORMAT

```
QUALIFICATION: bug | not-a-bug | insufficient information

SOURCE
<where in the code/data/spec the behavior originates — file:line and/or data tag/row>

RULE
<the business/technical rule that explains the observed result>

EVIDENCE
- <file:line, spec row, or data tag/value> — <what it shows>
- ...

CONFIDENCE: confirmed | likely | hypothesis (needs more data)

DATA GAPS (if confidence < confirmed)
<what additional data or logs would settle it>

SUGGESTED FIX (only if QUALIFICATION: bug)
<minimal patch, if the root cause is in the code — omit if data-only or not a bug>
```
````
