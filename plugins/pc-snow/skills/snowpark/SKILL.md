---
name: snowpark
description: Snowpark Python API — DataFrame API (transformations, actions, lazy evaluation), type system, SQL-to-Snowpark translation, UDFs/UDTFs, stored procedure registration, exception handling. Use for writing or translating Snowpark Python code — for deploying Snowflake objects via snowflake.core (IaC), see the sibling `snowflake` skill instead.
user-invocable: false
metadata:
  source: Snowflake official docs (docs.snowflake.com) — distilled/rewritten content
  updated: 2026-08-25
---

# Snowpark Python API

Covers the Snowpark Python API itself: the DataFrame abstraction, type system, translating SQL to
Snowpark idioms, UDFs/UDTFs, stored procedure registration, and exception handling. For deploying
Snowflake objects (Task, Table, Warehouse, Dynamic Table, etc.) via `snowflake.core`, use the
sibling `snowflake` skill instead.

> Snowpark's docs live under a distinct context7 library from the rest of Snowflake —
> `libraryId: /websites/snowflake_en_developer-guide_snowpark` (`resolve-library-id` then
> `query-docs`). Query it before trusting training data on any version-sensitive construct
> (recently-added functions, `GROUP BY ALL`, etc.). The Snowpark version line (1.4x/1.5x) is
> decoupled from the `snowflake.core` line (1.1x), and the docs site only publishes under `/latest/`,
> so context7 reflects the latest version, not necessarily the deployed one. For a version-borderline
> construct, verify against the deployed `snowflake.snowpark.__version__`.
> Prefer the native DataFrame API over raw `session.sql()` wherever a Python equivalent exists.

## Reference

- `references/snowpark.md` — API surface overview, DataFrame API vs Snowpark pandas API (when
  pandas legitimately wins: dynamic pivot, pivot without aggregation), Session, DataFrame API
  (transformations, actions, column expressions), canonical vs deprecated (camelCase) method
  names, SQL↔Snowpark translation table, type system, Stored Procedure registration, UDFs/UDTFs,
  SnowflakeFile, AsyncJob, exception handling, gotchas.
