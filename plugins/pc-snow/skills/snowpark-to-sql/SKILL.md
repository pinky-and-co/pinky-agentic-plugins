---
name: snowpark-to-sql
description: "Translate Snowpark Python (DataFrame API) into the equivalent Snowflake SQL. Prefers Snowpark's own generated SQL (`df.queries`) as the deterministic oracle when the code can run; otherwise a static translation using the sibling `snowpark` skill's mapping in reverse. Use for code review or de-migrating Snowpark to SQL. For the reverse direction use `sql-to-snowpark`."
argument-hint: "<path-to-source.py | python-snippet> [output-path]"
user-invocable: true
allowed-tools: Read, Write, Edit, Bash(find:*), Bash(rtk find:*), Bash(ls:*), Bash(rtk ls:*), Bash(cat:*), Bash(rtk read:*)
metadata:
  source: original — driver over the `snowpark` skill reference
  updated: 2026-08-27
---

# Snowpark → SQL

Translate Snowpark Python into Snowflake SQL. This direction has a **deterministic oracle**:
Snowpark itself compiles the DataFrame plan to SQL. Prefer it over a hand translation whenever the
code can run.

## Arguments

`$ARGUMENTS`:

1. **source** — path to a `.py` file, or a Snowpark snippet pasted inline. **Required.**
2. **output** *(optional)* — path to write the `.sql` to. Absent → print inline.

Source missing or ambiguous → ask before doing anything.

## Approach — oracle first, translation second

1. **Oracle (preferred, when executable).** If a Snowpark session is available and the target
   DataFrame can be built, the authoritative SQL is what Snowpark emits — capture it instead of
   translating by hand:

   ```python
   df.queries["queries"]  # the exact SQL Snowpark will run (list; usually 1 SELECT)
   # or df.explain()      # the plan + SQL
   ```


2. **Static translation (default, no session).** When the code can't be run (a plain code review),
   read `${CLAUDE_PLUGIN_ROOT}/skills/snowpark/references/snowpark.md` and translate top-to-bottom
   using it **in reverse**:

   - **`## SQL ↔ Snowpark translation`** read right-to-left
     (`join_table_function(flatten(col("X")))` → `LATERAL FLATTEN(X)`;
     `rank().over(Window.partition_by.order_by(desc))` filtered to `== 1` → `QUALIFY RANK() OVER (…) = 1`;
     the OUTER-FLATTEN coalesce/nullif workaround → `FLATTEN(…, OUTER => TRUE)`;
     `get_path(col, lit("a.b.c"))` → `v:"a"."b"."c"`;
     `to_decimal` / `to_boolean` → `::NUMBER(p,s)` / `::BOOLEAN`).
   - **`## Canonical vs deprecated method names`** — method → SQL clause.
   - `.cache_result()` is an execution boundary, not SQL — drop it (or render as a CTE / temp table
     if the author relied on materialization for window-then-filter correctness).

## Steps

1. **Read the source** in full; identify the DataFrame(s) and their build order.
2. **Decide oracle vs static** — say which you're using and why (no session → static).
3. **Produce SQL** — idiomatic Snowflake: CTEs for intermediate DataFrames, `QUALIFY` for windowed
   filters, `LATERAL FLATTEN` for table functions, VARIANT path syntax for `get` / `get_path`.
4. **Output** — write to `output` if given, else print inline. Use the session's scratchdir for any
   intermediate file, never `/tmp`.

## Notes

- **Honesty about the static path**: a hand translation may be *semantically equivalent* but not
  byte-identical to Snowpark's emitted SQL (alias names, parenthesisation, projected column order).
  When exactness matters, run the oracle (`df.queries`). Flag any spot you're unsure of rather than
  guessing.
- Version-sensitive constructs: verify against the doc for the Snowpark version in use, not from
  memory.
