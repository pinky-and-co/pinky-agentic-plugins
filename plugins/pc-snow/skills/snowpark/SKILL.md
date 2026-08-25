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
sibling `snowflake` skill instead — Snowpark and Core API are versioned and documented separately,
which is also why this is its own skill rather than a reference file under `snowflake`.

> Snowpark's docs live under a distinct context7 library from the rest of Snowflake —
> `libraryId: /websites/snowflake_en_developer-guide_snowpark` (`resolve-library-id` then
> `query-docs`). Query it before trusting training data on any version-sensitive construct
> (recently-added functions, `GROUP BY ALL`, etc.). A claim here can be stale for two independent
> reasons, not one: the Snowpark version line (1.4x/1.5x) is decoupled from the `snowflake.core`
> line (1.1x) — and the docs site only publishes under `/latest/`, no versioned URL, so context7
> itself reflects whatever was latest at query time, not necessarily what's deployed. If an
> environment pins Snowpark loosely (`snowflake-snowpark-python==*`, tracking the Anaconda
> channel), the live doc is a close reference for roughly that build, not a strict pin —
> illustratively, one observed pairing was a deployed `__version__` of 1.47.0 against a docs
> capture already at 1.48.0. For a genuinely version-borderline construct, verify against the
> deployed `snowflake.snowpark.__version__` directly rather than assuming the doc matches exactly.
> Prefer the native DataFrame API over raw `session.sql()` wherever a Python equivalent exists.

## Reference

- `references/snowpark.md` — API surface overview, DataFrame API vs Snowpark pandas API (when
  pandas legitimately wins: dynamic pivot, pivot without aggregation), Session, DataFrame API
  (transformations, actions, column expressions), canonical vs deprecated (camelCase) method
  names, SQL↔Snowpark translation table, type system, Stored Procedure registration, UDFs/UDTFs,
  SnowflakeFile, AsyncJob, exception handling, gotchas.
