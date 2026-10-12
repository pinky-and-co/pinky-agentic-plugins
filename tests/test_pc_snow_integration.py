"""Live, read-only calls of the pc-snow tools on the default Snowflake connection.

Excluded from the default run (`addopts = -m 'not integration'`); run with
`uv run pytest -m integration -s`. The server starts through `uv run --script`, so Snowpark comes
from the script's own metadata and the repo's lock is untouched. No task, warehouse or profile name
is written anywhere: the test discovers them with `SHOW` at run time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pc_snow_stdio import StdioMcp  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("uv") is None, reason="uv is the server's prerequisite"
    ),
]

# Capturé à l'import : la suite remplace `HOME` par un dossier temporaire avant chaque test, et le
# serveur comme la sonde doivent lire les vrais fichiers de connexion.
LIVE_ENV = {k: v for k, v in os.environ.items() if k != "PC_SNOW_TIMEZONE"}
ROOT = Path(__file__).parent.parent
SERVER = ROOT / "plugins" / "pc-snow" / "mcp" / "pc_snow_server.py"
PROBE = Path(__file__).parent / "pc_snow_live_probe.py"
LAST_WEEK_MINUTES = 7 * 24 * 60 - 5
INTERVAL_DAYS = 6
HISTORY_SETTLE_SECONDS = 3


def _structured(result: dict[str, Any]) -> dict[str, Any]:
    """The JSON object a successful `tools/call` returned."""
    assert result.get("isError") is not True, result
    if result.get("structuredContent"):
        return dict(result["structuredContent"])
    return dict(json.loads(result["content"][0]["text"]))


def _run_probe(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the live probe script; a failure carries the probe's own stderr."""
    done = subprocess.run(  # noqa: S603
        ["uv", "run", "--script", str(PROBE), *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=600,
        env=LIVE_ENV,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done


@contextmanager
def _server_with(extra_env: dict[str, str]) -> Iterator[StdioMcp]:
    """A second real server whose environment carries `extra_env`."""
    client = StdioMcp(["uv", "run", "--script", str(SERVER)], {**LIVE_ENV, **extra_env})
    client.initialize()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def server() -> Iterator[StdioMcp]:
    """The real server, started the way `.mcp.json` starts it."""
    started = time.monotonic()
    client = StdioMcp(["uv", "run", "--script", str(SERVER)], LIVE_ENV)
    client.initialize()
    client.list_tools()
    print(
        f"\nserver start + initialize + tools/list: {time.monotonic() - started:.1f}s"
    )
    yield client
    client.close()
    for line in client.stdout_lines:
        assert json.loads(line)["jsonrpc"] == "2.0"


@pytest.fixture(scope="module")
def visible() -> dict[str, Any]:
    """The tasks and warehouses the default connection can see."""
    done = _run_probe("discover")
    found = dict(json.loads(done.stdout))
    print(
        f"\nvisible: {found['task_count']} task(s), {len(found['warehouses'])} warehouse(s), "
        f"{len(found['databases'])} database(s)"
    )
    granted = [r for r in json.loads(found["secondary_roles"])["roles"].split(",") if r]
    print(
        f"primary role set: {bool(found['primary_role'])}; "
        f"secondary roles granted: {len(granted)}"
    )
    return found


def test_tools_are_announced_before_any_connection_is_used(server: StdioMcp) -> None:
    names = {tool["name"] for tool in server.list_tools()}
    assert names == {"task_history", "query_history"}


def test_task_history_statement_is_accepted_for_a_visible_task(
    server: StdioMcp, visible: dict[str, Any]
) -> None:
    if not visible["tasks"]:
        pytest.skip("the connection sees no task")
    task = visible["tasks"][0]
    result = _structured(
        server.call_tool(
            "task_history",
            {
                "database": task["database"],
                "schema": task["schema"],
                "task": task["name"],
                "last_minutes": LAST_WEEK_MINUTES,
            },
        )
    )
    print(f"\ntask_history on one visible task: {result['row_count']} row(s)")
    assert result["source"].endswith("INFORMATION_SCHEMA.TASK_HISTORY")
    assert result["fraicheur"]
    assert result["row_count"] <= result["limit"]
    assert result["timezone"]
    assert datetime.fromisoformat(result["range"]["start"]).utcoffset() is not None
    with_text = _structured(
        server.call_tool(
            "task_history",
            {
                "database": task["database"],
                "schema": task["schema"],
                "task": task["name"],
                "last_minutes": LAST_WEEK_MINUTES,
                "include_text": True,
            },
        )
    )
    print(f"task_history with include_text: {with_text['row_count']} row(s)")
    assert with_text["row_count"] == result["row_count"]


def test_task_history_returns_bounded_rows_with_source_and_zone(
    server: StdioMcp, visible: dict[str, Any]
) -> None:
    for task in visible["tasks"]:
        result = _structured(
            server.call_tool(
                "task_history",
                {
                    "database": task["database"],
                    "schema": task["schema"],
                    "task": task["name"],
                    "last_minutes": LAST_WEEK_MINUTES,
                    "limit": 5,
                },
            )
        )
        if result["row_count"]:
            break
    else:
        pytest.skip("no visible task has executions in the last 7 days")
    print(
        f"\ntask_history: {result['row_count']} row(s), truncated={result['truncated']}"
    )
    assert 0 < result["row_count"] <= 5
    assert result["source"]
    assert result["fraicheur"]
    assert result["range"]["start"][-6] in "+-"
    stamp = result["rows"][0]["SCHEDULED_TIME"]
    assert datetime.fromisoformat(stamp).utcoffset() is not None
    assert all(row["NAME"].upper() == task["name"].upper() for row in result["rows"])
    assert all("ERROR_MESSAGE" not in row for row in result["rows"])


def test_query_history_by_warehouse_then_by_id(
    server: StdioMcp, visible: dict[str, Any]
) -> None:
    for warehouse in visible["warehouses"]:
        by_warehouse = _structured(
            server.call_tool(
                "query_history",
                {"warehouse": warehouse, "last_minutes": 24 * 60, "limit": 5},
            )
        )
        if by_warehouse["row_count"]:
            break
    else:
        pytest.skip("no visible warehouse ran a query in the last day")
    print(f"\nquery_history by warehouse: {by_warehouse['row_count']} row(s)")
    assert 0 < by_warehouse["row_count"] <= 5
    assert by_warehouse["source"]
    target = by_warehouse["rows"][0]["QUERY_ID"]
    by_id = _structured(
        server.call_tool("query_history", {"query_id": target, "warehouse": warehouse})
    )
    print(f"query_history by id: {by_id['row_count']} row(s)")
    assert [row["QUERY_ID"] for row in by_id["rows"]] == [target]
    alone = _structured(server.call_tool("query_history", {"query_id": target}))
    print(f"query_history by id, no warehouse: {alone['row_count']} row(s)")
    assert alone["source"] == "SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY"
    assert alone["row_count"] in (0, 1)
    with_text = _structured(
        server.call_tool(
            "query_history",
            {
                "warehouse": warehouse,
                "last_minutes": 24 * 60,
                "limit": 5,
                "include_text": True,
            },
        )
    )
    print(f"query_history with include_text: {with_text['row_count']} row(s)")
    assert with_text["row_count"] > 0
    assert all("QUERY_TEXT" in row for row in with_text["rows"])


def test_the_default_connection_variable_selects_the_connection(
    visible: dict[str, Any],
) -> None:
    arguments = {"warehouse": visible["warehouses"][0], "last_minutes": 5, "limit": 1}
    variable = "SNOWFLAKE_DEFAULT_CONNECTION_NAME"
    with _server_with({variable: visible["default_profile"]}) as named:
        accepted = _structured(named.call_tool("query_history", arguments))
    print(f"\nvariable set to the default profile: {accepted['row_count']} row(s)")
    bogus = "no-such-profile-for-pc-snow-tests"
    with _server_with({variable: bogus}) as refused:
        outcome = refused.call_tool("query_history", arguments)
    print(f"variable set to a bogus profile: isError={outcome['isError']}")
    assert outcome["isError"] is True
    assert bogus in outcome["content"][0]["text"]


def test_a_call_without_scope_is_an_instruction_not_a_query(server: StdioMcp) -> None:
    result = server.call_tool("task_history", {})
    assert result["isError"] is True
    assert "Ask the user" in result["content"][0]["text"]


def test_same_interval_selects_the_same_queries_under_any_session_timezone(
    visible: dict[str, Any], server: StdioMcp
) -> None:
    # Chaque appel de l'outil est lui-même une requête sur l'entrepôt de la session : ceux-ci
    # garantissent des lignes dans l'intervalle, qui se termine avant le premier appel comparé.
    for warehouse in visible["warehouses"]:
        server.call_tool(
            "query_history", {"warehouse": warehouse, "last_minutes": 5, "limit": 1}
        )
    time.sleep(HISTORY_SETTLE_SECONDS)
    end = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    start = end - timedelta(days=INTERVAL_DAYS)
    for warehouse in visible["warehouses"]:
        busy = _structured(
            server.call_tool(
                "query_history",
                {
                    "warehouse": warehouse,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": 1,
                },
            )
        )
        if busy["row_count"]:
            break
    else:
        pytest.skip("no visible warehouse ran a query in the interval")
    done = _run_probe("timezone", warehouse, start.isoformat(), end.isoformat())
    outcome = json.loads(done.stdout)
    zones = sorted(outcome)
    assert [outcome[z]["applied_timezone"] for z in zones] == zones
    first, second = (outcome[z]["query_ids"] for z in zones)
    print(
        f"\nsame interval under two session timezones: {len(first)} and {len(second)} query id(s)"
    )
    assert first
    assert first == second


def test_stdout_of_the_live_server_is_protocol_only(server: StdioMcp) -> None:
    assert server.stdout_lines
    for line in server.stdout_lines:
        assert json.loads(line)["jsonrpc"] == "2.0"
