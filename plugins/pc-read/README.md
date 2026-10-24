# pc-read

Cheaper reads of heavy local documents: converts a `.pdf`, `.docx`, `.pptx`, `.xlsx` or `.html` to
Markdown (document-to-md), and a band above the prompt to convert, read as-is or deny a native
`Read` of one.

## Overview

The plugin ships a `PreToolUse` hook that asks before a native `Read` of an expensive format, a band
above the prompt that turns that question into buttons, and two skills: `document-to-md` converts a
document to Markdown first, `data-schema` reads a row-holding file by its schema instead of its
cells. `pc-core` does not carry any of this and does not depend on `pc-read`: install `pc-read` to
get the guard and the skills (`pc-read:document-to-md`, `pc-read:data-schema`).

## Skills

### `data-schema`

Reads a data file — `.xlsx`, `.csv` or `.parquet` that holds rows rather than prose — as its sheets,
columns with their types and at most 5 sample rows per sheet, instead of every cell
(`skills/data-schema/scripts/data_schema.py`, `uv run --script`, PEP 723 dependencies). The sample
does not go through `Read`, so the script asks `pc-privacy`'s `pii_guard.py --verdict` first (the
folder given by `--privacy-root`, else the `pc-privacy` entry of `installed_plugins.json`): a
flagged file, or a guard that fails, times out or answers something else than JSON, gives the schema
alone; no `pc-privacy` gives the sample. Types come from the first 100 non-empty rows (a `.parquet`
states its own); row counts are not reported; an `.xlsx` of prose (specs, a data dictionary) is for
`document-to-md`.

```
/pc-read:data-schema ~/data/exports/orders.xlsx
```

### `document-to-md`

Reads a heavy or binary local document — `.docx`, `.pptx`, `.xlsx`, `.html`, or a text-only `.pdf` —
by converting it to Markdown with `markitdown` and reading that, in the calling session. Local
files only: a URL pointing at a binary is not fetchable. Without `markitdown` it falls back to
`Read` and says once how to install it (`uv tool install 'markitdown[all]'`); a scanned or
layout-critical `.pdf` is read natively, and a `.xlsx`/`.csv`/`.parquet` holding *data* goes to
`data-schema`.

```
Read the deck at ~/specs/kickoff.pptx
```

## Hooks

- `hooks/read_cost.py` — `PreToolUse` on every `Read` (timeout 10 s). What makes a read costly is
  the format — `.pdf`, `.html`, `.xlsx`, `.docx`, `.pptx` — with no size threshold: a `.pdf` or
  `.html` reads without error and lands whole in context, markup and all. A large `.csv` or `.log`
  does not convert into prose, so size would be another function. The gain depends on the format: a
  `.xlsx` refuses a native read (the conversion is then the only path); a `.pdf` or `.html` reads
  natively, markup included, and the saving there is not measured. The hook answers `ask`, never
  `deny` — a scanned or layout-dependent PDF genuinely needs a native read — and never raises, so a
  bug of its own cannot block a tool call.
- `hooks/read-cost-hold/register.tsx` — a mod, written for the Claude Code mods API (early access,
  measured on 2.1.288), that holds a `Read` the hook flags and draws a framed band above the
  prompt: `⚠︎ Costly to read as-is` and the file name, then one button per outcome, in this order
  for every format (the first press wins):
  - `⎘ convert to Markdown` (`1`) runs `markitdown` into a fresh private folder, then refuses the
    `Read` naming the Markdown file; the next read is of that file, whose suffix is not costly.
    Hidden when `markitdown` is not on the `PATH`: the band then says how to install it
    (`uv tool install 'markitdown[all]'`). A conversion that fails refuses the `Read` and says so.
  - `▤ schema + first rows` (`3`), for a `.xlsx` only: runs the `data-schema` script into a fresh
    private folder, then refuses the `Read` naming that Markdown (column types and at most 5 rows
    per sheet). `.csv` and `.parquet` are not held by the band; they use the skill directly.
  - `✔︎ read as-is` (`2`) lifts the hook's `ask` for this call only: the same file read again is
    held again.
  - `✘ deny` (`0`) refuses the `Read`; Claude asks what to do instead.

  When nobody answers within one minute, when the verdict cannot be read, or when the session draws
  on neither a terminal nor the desktop app (`claude -p`, the VS Code extension alone), nothing is
  held and the hook's `ask` applies as is.

  A file `pc-privacy` flags as personal data is not held here: the band steps aside and
  `pc-privacy`'s band decides. The mod finds that plugin's guard through the folder `pc-privacy`
  publishes in the shared state, and asks `pii_guard.py --verdict` before drawing its band, so a
  `.xlsx` or `.html` costs one more scan per `Read`; a real PDF or Office binary is skipped by the
  guard's format detection. That wait still needs a working `uv`: for a `.pdf`, `.docx` or `.pptx`
  too, a cold first run or a missing `uv` removes the band and the native dialog applies.

  - `pc-privacy` is absent (nothing published): the read is held as usual.
  - `pc-privacy` is published but its guard fails, times out, exits non-zero or prints something
    else than JSON: the band steps aside too — nothing is held or remembered, and the decision goes
    to `pc-privacy`'s band or to the native dialog.
  - Only a successful `flagged: false` answer lets this band hold the read.
  - Reading the shared state itself fails: the read is held as usual, so "read as-is" can then also
    lift `pii_guard`'s `ask`. This branch is not measured on a live host.

  The hook stays the source of the verdict: the mod asks `read_cost.py --verdict` and gets the
  Markdown folder from `read_cost.py --target <path>` (`--discard <markdown>` removes that folder
  after a failed conversion), never judging by itself. A read the engine denies or a settings rule
  decides is not held.

  The mod is TypeScript, which the repo's gates do not run. Check it with
  `claude plugin validate plugins/pc-read`, `claude plugin test plugins/pc-read` and
  `tsc -p plugins/pc-read` (the config it extends is written by Claude Code the first time the
  plugin loads from a folder you own).

## Requirements

- `python3` on the `PATH`, version 3.12 or newer — the hook and the band's `--verdict` calls
  (stdlib only).
- `uv` — `data_schema.py` runs as `uv run --script` (resolves its `openpyxl`/`pyarrow`
  dependencies), and the band's `pc-privacy` scan waits on it.
- `markitdown` (`uv tool install 'markitdown[all]'`; the `[all]` extra pulls the
  docx/pptx/xlsx/pdf handlers) — the conversion; the band hides the convert button without it.

## Dependencies

None.
