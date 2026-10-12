#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["mcp>=2", "snowflake-snowpark-python"]
# ///
"""pc-snow observability MCP server — `task_history` and `query_history` as native tools.

Each tool is a thin wrapper around `pc_snow_tools.py`, which holds the scope, range and date
contract and receives the Snowflake session as a parameter. No session is opened and nothing is
asked of the user until the first tool call: `tools/list` answers without credentials.

Declared via `.mcp.json` at the plugin root (`command: uv, args: [run, --script, .../server.py]`) —
`uv` resolves `mcp` and `snowflake-snowpark-python` from this file's own inline metadata.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).parent))

import pc_snow_tools
from mcp.server import MCPServer
from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import create_model

m = MCPServer("pc-snow")


def _session_builder() -> Any:
    """A fresh Snowpark session builder; Snowpark is imported here, on first use only."""
    from snowflake.snowpark import Session

    return Session.builder


connection = pc_snow_tools.Connection(_session_builder)


def _profile_asker(ctx: Context) -> pc_snow_tools.ProfileAsker:
    """Ask the user which connection profile to use, as a single choice among `profiles`."""

    async def ask(profiles: list[str]) -> str | None:
        """The profile the user picked, or `None` when the client cannot ask or the user declined."""
        capabilities = ctx.client_capabilities
        if capabilities is None or capabilities.elicitation is None:
            return None
        choice = create_model(
            "ConnectionProfile",
            profile=(Literal[tuple(profiles)], ...),  # type: ignore[valid-type]
        )
        try:
            outcome = await ctx.elicit(
                "Which Snowflake connection profile should pc-snow use?", choice
            )
        except Exception:
            # Le client n'a pas pu répondre (pas de canal retour, erreur) : même issue qu'un refus.
            return None
        if isinstance(outcome, AcceptedElicitation):
            return str(outcome.data.profile)
        return None

    return ask


@m.tool()
async def task_history(
    database: str | None = None,
    schema: str | None = None,
    task: str | None = None,
    start: str | None = None,
    end: str | None = None,
    last_minutes: int | None = None,
    limit: int = 50,
    include_text: bool = False,
    *,
    ctx: Context,
) -> dict[str, Any]:
    """Executions of one Snowflake task, from INFORMATION_SCHEMA.TASK_HISTORY (7 days, near real time).

    Required scope, to ask the user and never guess: database, schema, task (plain identifiers).
    Required range: start and end (ISO-8601, no offset = the user's timezone), or last_minutes
    (at most 10079). Never both. Timestamps carry their offset, which is authoritative: `timezone`
    is the PC_SNOW_TIMEZONE name, else the system abbreviation (e.g. CEST).
    limit: 1..200 rows (default 50), newest first; `truncated` tells whether more exist.
    Text fields are cut at 200 characters; error messages, return values and the task SQL are
    returned only with include_text=true. Returns source, fraicheur, range, rows.
    """
    try:
        request = pc_snow_tools.plan_task_history(
            database, schema, task, start, end, last_minutes, limit, include_text
        )
        return await pc_snow_tools.task_history(
            connection, _profile_asker(ctx), request
        )
    except pc_snow_tools.PcSnowError as exc:
        raise ToolError(str(exc)) from exc


@m.tool()
async def query_history(
    query_id: str | None = None,
    warehouse: str | None = None,
    start: str | None = None,
    end: str | None = None,
    last_minutes: int | None = None,
    limit: int = 50,
    include_text: bool = False,
    *,
    ctx: Context,
) -> dict[str, Any]:
    """Queries from INFORMATION_SCHEMA.QUERY_HISTORY (7 days, near real time), by id or by warehouse.

    Scope, to ask the user and never guess: a query_id, or a warehouse with a range. A query_id
    alone searches the whole 7-day window. Other users' queries need MONITOR or OPERATE on the
    warehouse or task. Range: start and end (ISO-8601, no offset = the user's timezone), or
    last_minutes (at most 10079), never both. Timestamps carry their offset (authoritative);
    `timezone` is the PC_SNOW_TIMEZONE name, else the system abbreviation (e.g. CEST).
    limit: 1..200 rows (default 50), newest first. Query text and error messages need
    include_text=true and are cut at 200 characters. Returns source, fraicheur, range, rows.
    """
    try:
        request = pc_snow_tools.plan_query_history(
            query_id, warehouse, start, end, last_minutes, limit, include_text
        )
        return await pc_snow_tools.query_history(
            connection, _profile_asker(ctx), request
        )
    except pc_snow_tools.PcSnowError as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    m.run()
