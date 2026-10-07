---
name: debug-brief
argument-hint: "<behavior discrepancy> --code <src…> --data <file…> [--spec <path>] [--send [model]]"
description: "Assemble a self-contained debug brief — a human-reported behavior discrepancy confronted against the code, an optional spec, and a sample of the real data — so a model can reason about root cause. Output is one text block to paste into an external model, or send through the panel MCP tool. Use when someone reports 'X does the wrong thing' and wants a rigorous bug/not-a-bug qualification before touching the code."
user-invocable: true
allowed-tools: Read, Bash(find:*), Bash(rtk find:*), Bash(ls:*), Bash(rtk ls:*), Skill
metadata:
  source: original
  updated: 2026-10-07
---

# Debug Brief

Assemble a self-contained debug brief. The brief confronts a **human-reported behavior discrepancy**
against the code, an optional spec, and a sample of the dataset the bug was reported against — so a
model can reason about root cause. The output is a single text block: no commentary, no preamble.

The point of the skill is the **qualify-first discipline** — is this actually a defect, or
intended/documented behavior the reporter isn't aware of?

## Preflight

**`--send` requested but `panel` unreachable or empty?** The `panel` MCP tool (`pc-core-models`
server) runs the brief through one non-Claude pool model (sonnet/opus-tier entries only). If
`--send` was passed and `panel` is missing from your tool list, or it returns `BLOCKED` (no
eligible pool entry), produce the brief block instead and say the send step was skipped.

## Data boundary — the guard decides

Read each `--data` file like any other file. Personal data is the business of the privacy guard, not
of this skill: when `pc-privacy` is installed, it intercepts a `Read` of a file holding personal data
and offers to anonymize it into a `<name>.anon.<ext>` copy, allow the read, or refuse it. Anonymizing
is the person's choice.

- The person anonymized: read the `*.anon.*` copy, not the original.
- The person allowed the read: use the file as read.
- The person refused: ask what to do instead. Never work around a refusal (a copy, `cat`, another
  path).

## Arguments

`$ARGUMENTS` — a human description of the unexpected behavior (inline text, or a path to a file
containing it), plus:

| Flag | Description |
|---|---|
| `--code <path...>` | One or more source files implicated in the bug (required) |
| `--spec <path>` | Optional spec / ADR / ticket describing the *expected* behavior |
| `--data <path...>` | One or more CSV/JSON/Parquet/XML files to include as the data sample (required) |
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
1. Read `--spec` in full if given.
1. Read each `--data` file, under the data boundary above.

## Step 2 — Deliver

**If `--send [model]` is present:** first, if any DATA file was read as the original rather than as
a `*.anon.*` copy, say so and ask for confirmation — the brief leaves the machine. Then call the
`panel` MCP tool with a single task —
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

## DATA (sample)

<for each --data file as read (the `*.anon.*` copy when the person anonymized it): a filename header,
then full content — if a file exceeds ~200 rows, include the first 100 + a note "(truncated — N rows
total, ask for a specific range if needed)">

Anonymized: <yes or no, per file>. Untouched columns carry the actual debugging signal (dates,
amounts, statuses, non-identifying IDs) — treat them as ground truth.

---

## ROLE

You are a senior engineer debugging a reported behavior discrepancy. You have the code, optionally a
spec of the *intended* behavior, and a sample of the data the bug was reported against. Reason from
evidence in the data + code — do not invent behavior neither shows. **Qualify first**: is this
actually a defect, or intended/documented behavior the reporter isn't aware of?

---

## GUIDELINES

- **Do not assume it's a bug.** Qualify before explaining — a well-reasoned wrong verdict is worse
  than an admitted "insufficient information," and harder to catch on review.
- **Ground every claim in a specific line, tag, or row.** A claim with no citation is a guess —
  label it as such.
- **Distinguish confirmed from hypothesis.** If the material doesn't prove the mechanism, say what
  additional data or logs would confirm or rule it out — do not fill the gap with invented behavior.
- **Anonymized values are opaque tokens, not noise.** Two rows sharing the same `EMAIL_xxxx` token
  are the same real customer — use that for cross-row reasoning exactly as you would the real value.
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
