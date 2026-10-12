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

Both are fixed, read-only queries with bound arguments — no SQL goes in. Rows are bounded (50 by
default, 200 at most), text fields are cut at 200 characters, and free text (query text, error
messages) is returned only with `include_text=true`. The interpretation of each column lives in the
`snowflake` skill's `references/system-views.md`.

Connection and settings, all on the user's side:

- The default connection of the machine is used: `default_connection_name` in `config.toml`, or
  the connector's own `SNOWFLAKE_DEFAULT_CONNECTION_NAME`, which selects another profile.
  With several profiles and no default, the server asks which one to use; when it cannot ask, the
  error names `SNOWFLAKE_DEFAULT_CONNECTION_NAME`.
- `PC_SNOW_TIMEZONE` (an IANA name) sets the timezone dates are read and rendered in; the system
  timezone otherwise. The Snowflake session's `TIMEZONE` is never used.
- Secondary roles are activated once when the session opens; `USE ROLE` is never issued.
