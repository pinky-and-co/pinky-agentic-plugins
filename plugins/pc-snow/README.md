# pc-snow

Snowflake anti-training-bias knowledge — public.

## Overview

Training data drifts behind Snowflake's actual release cadence: each skill in this plugin covers
one Snowflake topic (platform/IaC, Snowpark, Cortex, Streamlit in Snowflake), distilled from
official Snowflake documentation with provenance tracked per file (`provenance-attribution.md`
convention), and is read before working on that topic rather than relied on from memory. Two of
the skills are user-invocable translation drivers between SQL and Snowpark; the rest are
reference knowledge. An MCP server adds four read-only observability tools over a live Snowflake
account. Generic across any Snowflake project.

## Skills

### `cortex` *(reference)*

Snowflake Cortex — AI SQL/Snowpark functions (`AI_EXTRACT`, `AI_COMPLETE`, `AI_CLASSIFY`,
`AI_FILTER`, guardrails, arctic-extract fine-tuning) and the managed Cortex Search service
(hybrid vector + lexical RAG). Use for document extraction, classification, LLM guardrails, or
semantic search/RAG on Snowflake.

### `snowflake` *(reference)*

Snowflake platform reference: Python Core API (`snowflake.core`) for IaC deployment,
warehouse/compute-pool sizing and caching, PyPI packages in UDFs/procedures, governance
(classification, tags, masking, row access, lineage, GDPR), telemetry/event tables, cost model
and tracking, the Native App framework, and task and query history through the pc-snow tools.
Use for Snowflake deployment, sizing, governance, telemetry, cost, Native Apps or diagnosing a
task or a query.

### `snowpark` *(reference)*

Snowpark Python API — DataFrame API (transformations, actions, lazy evaluation), type system,
SQL-to-Snowpark translation, UDFs/UDTFs, stored procedure registration, exception handling. For
deploying Snowflake objects via `snowflake.core` (IaC), use the sibling `snowflake` skill
instead.

### `snowpark-to-sql`

Translate Snowpark Python (DataFrame API) into the equivalent Snowflake SQL. Prefers Snowpark's
own generated SQL (`df.queries`) as the deterministic oracle when the code can run; otherwise a
static translation using the sibling `snowpark` skill's mapping in reverse. For code review or
de-migrating Snowpark to SQL; the reverse direction is `sql-to-snowpark`.

```
snowpark-to-sql jobs/build_daily_aggregate.py daily_aggregate.sql
```

### `sql-to-snowpark`

Translate a Snowflake SQL query into the Snowpark Python DataFrame API. Thin driver — the
SQL↔Snowpark mapping and the translation pitfalls live in the sibling `snowpark` skill's
reference; this applies them to your query, CTE by CTE. For migrating a SQL query or view to
Snowpark Python; the reverse direction is `snowpark-to-sql`.

```
sql-to-snowpark transforms/page_views.sql page_views.py
```

### `streamlit` *(reference)*

Streamlit in Snowflake (SiS) — Container vs Standard runtime, SiS-specific APIs
(`st.connection`, secrets, caller's rights), partial reruns (`st.fragment`), cache layering
against cold starts, layout/CSS constraints, upgrade evaluation methodology. For building or
upgrading a Streamlit app that runs inside Snowflake.

## MCP tools

`mcp/pc_snow_server.py` is a stdio MCP server declared by `.mcp.json`. It runs through
`uv run --script` and resolves its own dependencies (`mcp`, `snowflake-snowpark-python`) from
its inline metadata. The server opens no Snowflake session and asks nothing before the first
tool call: listing its tools needs no credentials.

- `task_history` — task runs from `INFORMATION_SCHEMA.TASK_HISTORY`. Scope: `database`,
  `schema`, `task`. Range: `start` + `end`, or `last_minutes`.
- `query_history` — queries from `INFORMATION_SCHEMA.QUERY_HISTORY`. Scope: `query_id`, or
  `warehouse`. Range: `start` + `end`, or `last_minutes`; optional with a `query_id`.
- `warehouse_credits` — credit consumption from
  `INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY`. Scope: `warehouse`, or `all_warehouses=true`.
  Range: `start` + `end`, or `last_minutes`. Needs `ACCOUNTADMIN` or the `MONITOR USAGE`
  privilege — without either, Snowflake answers no row and no error (measured), which the
  result's notes say.
- `event_table_records` — records from the event table that `EVENT_TABLE` names (schema, else
  database, else account). Scope: `database`; `schema`, `query_id` optional. Range: `start` +
  `end`, or `last_minutes`. Never takes a table name, and needs `SELECT` on the event table.

Shared contract:

- All four are fixed, read-only queries with bound arguments — no SQL goes in.
- Rows are bounded (50 by default, 200 at most), text fields are cut at 200 characters, and free
  text (query text, error messages, log bodies) is returned only with `include_text=true`.
- `task_history` and `query_history` read `ACCOUNT_USAGE` (delayed, 365 days) for a range that
  starts over 7 days ago; it needs a `SNOWFLAKE` database role per view (`USAGE_VIEWER` for
  tasks, `GOVERNANCE_VIEWER` for queries), and the error names the `GRANT` of the view it
  refused.
- The interpretation of each column lives in the `snowflake` skill's
  `references/system-views.md`.

Connection and settings, all on the user's side:

- The default connection of the machine is used: `default_connection_name` in `config.toml`, or
  the connector's own `SNOWFLAKE_DEFAULT_CONNECTION_NAME`, which selects another profile. With
  several profiles and no default, the server asks which one to use; when it cannot ask, the
  error names `SNOWFLAKE_DEFAULT_CONNECTION_NAME`.
- `PC_SNOW_TIMEZONE` (an IANA name) sets the timezone dates are read and rendered in; the system
  timezone otherwise. The Snowflake session's `TIMEZONE` is never used.
- Secondary roles are activated once when the session opens; `USE ROLE` is never issued.

## Requirements

- `python3` 3.12–3.13 (the MCP server's inline metadata pins `>=3.12,<3.14`).
- `uv` — a prerequisite for the MCP server, on top of Python: `uv run --script` resolves `mcp`
  and `snowflake-snowpark-python` from the server file's inline metadata; the plugin ships no
  other dependency mechanism.
- A Snowflake account connection for actual tool calls (the machine's default connection, as
  above) with the privileges each tool needs; listing the tools needs no credentials. The skills
  themselves are prompt-only and need none of this.

## Dependencies

None.
