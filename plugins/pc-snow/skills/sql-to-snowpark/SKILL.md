---
name: sql-to-snowpark
description: "Translate a Snowflake SQL query into the Snowpark Python DataFrame API. Thin driver — the SQL↔Snowpark mapping and the translation pitfalls live in the sibling `snowpark` skill's reference; this applies them to your query, CTE by CTE. Use when migrating a SQL query or view to Snowpark Python. For the reverse direction use `snowpark-to-sql`."
argument-hint: "<path-to-source.sql | sql-snippet> [output-path]"
user-invocable: true
allowed-tools: Read, Write, Edit, Bash(find:*), Bash(rtk find:*), Bash(ls:*), Bash(rtk ls:*), Bash(cat:*), Bash(rtk read:*)
metadata:
  source: original — driver over the `snowpark` skill reference
  updated: 2026-08-27
---

# SQL → Snowpark

Translate a Snowflake SQL query into Snowpark Python. The SQL↔Snowpark mapping and the gotchas live
in the `snowpark` knowledge skill; this thin driver reads it and applies it to *your* query.

## Arguments

`$ARGUMENTS`:

1. **source** — path to a `.sql` file, or the SQL pasted inline. **Required.**
2. **output** *(optional)* — path to write the Snowpark Python to. Absent → print inline.

Source missing or ambiguous → ask before doing anything.

## Translation reference — read first

Read `${CLAUDE_PLUGIN_ROOT}/skills/snowpark/references/snowpark.md` before translating —
specifically:

- **`## SQL ↔ Snowpark translation`** — the canonical construct mapping (FLATTEN/OUTER,
  `QUALIFY` → `rank().over`, `GROUP BY ALL`, VARIANT `get_path`, `convert_timezone`, `NULLIFZERO`,
  `ILIKE`, `sql_expr`, …).
- **`## Gotchas` → `### Translation pitfalls (SQL → Snowpark)`** — VARIANT-before-`.isin`,
  `concat_ws` optional fields, `startswith` needs `lit`, `.isin` returns bool, Pylance annotations.
- **`## Canonical vs deprecated method names`** — snake_case API (camelCase is banned /
  hook-enforced).

Do **not** restate the mapping table back to the user — apply it.

## Steps

1. **Read the SQL** (file or inline) in full.
2. **Analyze** and note: CTEs and their dependency order; the filter column / parameter (the
   `WHERE … IN (…)` driver), if any; source tables and databases; every construct that hits a row
   in the reference's mapping or pitfalls table.
3. **Present the plan first** — CTE list, filter column, complex patterns detected — and **wait for
   confirmation** if anything is ambiguous (an unclear filter column, an unsupported construct, a
   VARIANT path you had to guess). Otherwise proceed.
4. **Translate** CTE by CTE into Snowpark, applying the reference's mapping and pitfalls. Annotate
   every intermediate DataFrame `: snowpark.DataFrame`. Import only what you use (`ruff` blocks
   unused).
5. **Output** — write to the `output` path if given (follow any existing structure/conventions of
   that file), else print the Python inline. Use the session's scratchdir for any intermediate
   file, never `/tmp`.

## Notes

- Provider/instance-specific orchestration (a fixed SP template, a repo layout, a post-migration
  README step) is **not** part of this generic skill — wrap that in a private profile skill if you
  need it.
