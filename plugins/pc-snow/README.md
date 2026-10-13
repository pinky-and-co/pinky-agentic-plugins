# pc-snow

Snowflake anti-training-bias knowledge, distilled from official Snowflake documentation with
provenance tracked per file (`provenance-attribution.md` convention) — public, generic across any
Snowflake project.

## Overview

Training data drifts behind Snowflake's actual release cadence. Each skill in this plugin covers
one Snowflake topic (Core API, Snowpark, warehouses, Cortex, governance, …), read before working
on that topic rather than relied on from memory.

## Skills

None yet.

## MCP server

`mcp/pc_snow_server.py` is a stdio MCP server declared by `.mcp.json`. It runs through
`uv run --script` and resolves its own dependencies (`mcp`, `snowflake-snowpark-python`) from its
inline metadata, so **`uv` is a prerequisite** on top of Python. The server opens no Snowflake
session and asks nothing before the first tool call: listing its tools needs no credentials.

| Tool | Reads | Scope (ask the user) | Range |
|---|---|---|---|
| `task_history` | `INFORMATION_SCHEMA.TASK_HISTORY` | `database`, `schema`, `task` | `start` + `end`, or `last_minutes` |
| `query_history` | `INFORMATION_SCHEMA.QUERY_HISTORY` | `query_id`, or `warehouse` | `start` + `end`, or `last_minutes`; optional with a `query_id` |
| `warehouse_credits` | `INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY` | `warehouse`, or `all_warehouses=true` | `start` + `end`, or `last_minutes` |
| `event_table_records` | the event table `EVENT_TABLE` names (schema, else database, else account) | `database`; `schema`, `query_id` optional | `start` + `end`, or `last_minutes` |

All are fixed, read-only queries with bound arguments — no SQL goes in. Rows are bounded (50 by
default, 200 at most), text fields are cut at 200 characters, and free text (query text, error
messages, log bodies) is returned only with `include_text=true`. `warehouse_credits` needs
`ACCOUNTADMIN` or the `MONITOR USAGE` privilege, and without either Snowflake answers no row and
no error (measured), which the result's notes say; `event_table_records` never takes a table name —
the table is the one `EVENT_TABLE` designates, and it needs `SELECT` on it. `task_history` and
`query_history` read `ACCOUNT_USAGE` (delayed, 365 days) for a range that starts over 7 days ago; it
needs a `SNOWFLAKE` database role per view (`USAGE_VIEWER` for tasks, `GOVERNANCE_VIEWER` for
queries), and the error names the `GRANT` of the view it refused. The interpretation of each column lives in the
`snowflake` skill's `references/system-views.md`.

Connection and settings, all on the user's side:

- The default connection of the machine is used: `default_connection_name` in `config.toml`, or
  the connector's own `SNOWFLAKE_DEFAULT_CONNECTION_NAME`, which selects another profile.
  With several profiles and no default, the server asks which one to use; when it cannot ask, the
  error names `SNOWFLAKE_DEFAULT_CONNECTION_NAME`.
- `PC_SNOW_TIMEZONE` (an IANA name) sets the timezone dates are read and rendered in; the system
  timezone otherwise. The Snowflake session's `TIMEZONE` is never used.
- Secondary roles are activated once when the session opens; `USE ROLE` is never issued.
