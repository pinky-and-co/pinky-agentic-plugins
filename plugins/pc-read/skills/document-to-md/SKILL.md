---
name: document-to-md
description: Read a heavy or binary local document — .docx, .pptx, .xlsx, .html, or a text-only .pdf — by converting it to Markdown first. Use whenever the user points at such a file on disk. Not for remote URLs, and not for spreadsheets that hold data rather than prose.
argument-hint: "<path-to-document>"
allowed-tools: Bash(command -v markitdown:*), Bash(mktemp:*), Bash(markitdown:*), Read
---

# Read a document by converting it first

Runs in the calling session on purpose — no fork: **you** must end up holding the content.

## Step 1 — Is the conversion warranted?

| Format | Do |
|---|---|
| `.docx` `.pptx` `.xlsx` `.html` | **convert** — these cannot be read natively at all |
| text-only `.pdf` | **convert** — native read works, Markdown is just lighter |
| scanned / visual / layout-critical `.pdf` | **`Read` directly** — conversion mangles tables and OCRs scans |
| `.csv` `.parquet`, or an `.xlsx` holding *data* | **neither** — see the carve-out below |

**The carve-out that matters: a document to read is not a dataset to query.** An `.xlsx` of specs or
a data dictionary is prose, so it converts. An `.xlsx`/`.csv`/`.parquet` of *rows* must not be
flattened into Markdown prose — query it (DuckDB, or `pinky dump` on this instance).

## Step 2 — Check the tool exists, then convert

```
command -v markitdown
```

**Present** → make a fresh private folder (a path derived from the file name alone would collide
for two files sharing a name in different folders), convert into it, then `Read` the Markdown:

```
mktemp -d
markitdown <file> -o <folder printed above>/<name>.md
```

**Absent** → do not install it. Fall back to `Read` on the original: it handles PDFs natively, and
fails cleanly on the other formats. Then tell the user, once, that `markitdown` would make this
cheaper and how to get it — `uv tool install 'markitdown[all]'` or `pipx install 'markitdown[all]'`
(the `[all]` extra is what pulls the docx/pptx/xlsx/pdf handlers; the bare package omits them).
Installing software on someone's machine is their call, not yours.

## Step 3 — Read what you produced, not what you expected

Conversion is lossy on complex tables. If the content you need is tabular or layout-dependent and
the Markdown looks mangled, say so and re-read the original rather than reasoning on a broken table.

> Local files only. A URL pointing at a binary — PDF, archive, office file — is not fetchable:
> `WebFetch` reads public HTML/JSON pages. Ask for the file itself or for the content pasted.
