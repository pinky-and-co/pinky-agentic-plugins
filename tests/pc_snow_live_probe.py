#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["snowflake-snowpark-python"]
# ///
"""Read-only probes run by `test_pc_snow_integration.py` on the default Snowflake connection.

Two commands, each printing one JSON document on stdout:

- `discover` lists the databases, tasks and warehouses the connection can see (`SHOW`), so the integration
  test never needs a name written down anywhere.
- `timezone <warehouse> <start> <end>` runs the sibling module's `query_history` on two sessions
  whose `TIMEZONE` parameter differs and prints the query ids each one returned.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-snow" / "mcp"))

import pc_snow_tools
from snowflake.snowpark import Session

SESSION_TIMEZONES = ["America/Los_Angeles", "Pacific/Auckland"]
LISTED = 50


def _builder() -> Any:
    """A fresh Snowpark session builder."""
    return Session.builder


def discover() -> dict[str, Any]:
    """The tasks and warehouses visible to the connection, bounded."""
    connection = pc_snow_tools.Connection(_builder)
    session = asyncio.run(connection.session(None))
    tasks = session.sql("SHOW TASKS IN ACCOUNT").collect()
    warehouses = session.sql("SHOW WAREHOUSES").collect()
    databases = session.sql("SHOW DATABASES").collect()
    roles = session.sql(
        "SELECT CURRENT_ROLE() AS PRIMARY_ROLE, CURRENT_SECONDARY_ROLES() AS SECONDARY_ROLES"
    ).collect()[0]
    return {
        "databases": [d["name"] for d in databases[:LISTED]],
        "default_profile": _default_profile(),
        "primary_role": roles["PRIMARY_ROLE"],
        "secondary_roles": roles["SECONDARY_ROLES"],
        "task_count": len(tasks),
        "tasks": [
            {
                "database": t["database_name"],
                "schema": t["schema_name"],
                "name": t["name"],
            }
            for t in tasks[:LISTED]
        ],
        "warehouses": [w["name"] for w in warehouses[:LISTED]],
    }


def _default_profile() -> str:
    """The profile the default connection resolves to, read at run time."""
    from snowflake.connector.config_manager import CONFIG_MANAGER

    return str(CONFIG_MANAGER["default_connection_name"])


def timezone(warehouse: str, start: str, end: str) -> dict[str, Any]:
    """The same absolute interval, read under two different session timezones."""
    profile = _default_profile()
    outcome: dict[str, Any] = {}
    for zone in SESSION_TIMEZONES:
        session = Session.builder.configs(
            {"connection_name": profile, "session_parameters": {"TIMEZONE": zone}}
        ).create()
        try:
            session.sql("USE SECONDARY ROLES ALL").collect()
            applied = session.sql(
                "SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION"
            ).collect()
            request = pc_snow_tools.plan_query_history(
                query_id=None,
                warehouse=warehouse,
                start=start,
                end=end,
                last_minutes=None,
                limit=pc_snow_tools.MAX_LIMIT,
                include_text=False,
            )
            result = pc_snow_tools.query_history_on(session, request)
            outcome[zone] = {
                "applied_timezone": applied[0]["value"],
                "query_ids": [row["QUERY_ID"] for row in result["rows"]],
            }
        finally:
            session.close()
    return outcome


def main(argv: list[str]) -> int:
    """Run the requested probe and print its JSON result."""
    if argv[:1] == ["discover"]:
        print(json.dumps(discover()))
        return 0
    if argv[:1] == ["timezone"] and len(argv) == 4:
        print(json.dumps(timezone(argv[1], argv[2], argv[3])))
        return 0
    print("usage: discover | timezone <warehouse> <start> <end>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
