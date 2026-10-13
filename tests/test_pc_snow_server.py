"""Tests for the pc-snow MCP server: the tool logic against a fake session, the server over stdio.

The cross-cutting tests are parametrized on `TOOL_CASES`, one entry per tool. A tool added to the
server without an entry fails `test_every_server_tool_has_a_case`, so each new tool inherits the
scope, range, date, statement, bound and truncation checks by being listed there.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-snow"
sys.path.insert(0, str(PLUGIN / "mcp"))
sys.path.insert(0, str(Path(__file__).parent))

import pc_snow_tools as tools  # noqa: E402
from pc_snow_stdio import StdioMcp  # noqa: E402

NOW_MS = 1_000_000_000_000  # 2001-09-09T01:46:40Z, far from the machine's own clock
TARGET_QUERY_ID = "01b7c0de-0000-1111-2222-333344445555"
NO_PRIVILEGE = "Insufficient privileges to operate on task 'MY_TASK'"
USAGE_PROBE = "SELECT 1 AS PROBE FROM SNOWFLAKE.ACCOUNT_USAGE.{view} LIMIT 1"
USAGE_VIEW = {"task_history": "TASK_HISTORY", "query_history": "QUERY_HISTORY"}
USAGE_ROLE = {
    "task_history": "SNOWFLAKE.USAGE_VIEWER",
    "query_history": "SNOWFLAKE.GOVERNANCE_VIEWER",
}
# What Snowflake answers to a role without access to the SNOWFLAKE database (measured).
USAGE_REFUSED = "SQL compilation error: Schema 'SNOWFLAKE.ACCOUNT_USAGE' does not exist or not authorized."


# ── Fakes: the outside world only ────────────────────────────────────────────────────────────────


class FakeRow(dict[str, Any]):
    """A result row with the `as_dict` of a Snowpark `Row`."""

    def as_dict(self) -> dict[str, Any]:
        """The row as a plain dict."""
        return dict(self)


class SqlRefused(Exception):
    """A Snowflake error carrying the attributes Snowpark's SQL exception has."""

    def __init__(self, message: str, sql_error_code: int) -> None:
        """Keep the message and the Snowflake error code."""
        super().__init__(message)
        self.message = message
        self.sql_error_code = sql_error_code


@dataclass
class Statement:
    """One statement the fake session received."""

    sql: str
    params: list[Any]


class _Collected:
    """What `session.sql(...)` returns: something with `collect()`."""

    def __init__(self, rows: list[FakeRow]) -> None:
        """Hold the rows to hand back."""
        self._rows = rows

    def collect(self) -> list[FakeRow]:
        """The rows."""
        return self._rows


class FakeSession:
    """A Snowflake session that records statements and plays the history functions.

    `task_rows` / `query_rows` are newest first, as the functions return them. The functions apply
    `RESULT_LIMIT` (100 when the statement does not set it) before any `WHERE`, exactly as the
    real `QUERY_HISTORY` does, and the explicit `TASK_NAME` argument before it.
    """

    def __init__(
        self,
        task_rows: list[dict[str, Any]] | None = None,
        query_rows: list[dict[str, Any]] | None = None,
        credit_rows: list[dict[str, Any]] | None = None,
        event_rows: list[dict[str, Any]] | None = None,
        event_tables: dict[str, str | Exception] | None = None,
        fail_with: Exception | None = None,
        session_timezone: str = "Pacific/Auckland",
        fail_on_open: Exception | None = None,
        usage_outcome: Exception | dict[str, Exception] | None = None,
        databases: dict[str, list[str]] | None = None,
        show_failures: dict[str, Exception] | None = None,
    ) -> None:
        """Set the data the functions hold and an optional failure for the function statement."""
        self.task_rows = task_rows or []
        self.query_rows = query_rows or []
        self.credit_rows = credit_rows or []
        self.event_rows = event_rows or []
        # `EVENT_TABLE` by level; an exception is what Snowflake raises for that `SHOW PARAMETERS`.
        self.event_tables = (
            {"ACCOUNT": "SNOWFLAKE.TELEMETRY.EVENTS"}
            if event_tables is None
            else event_tables
        )
        self.fail_with = fail_with
        self.fail_on_open = fail_on_open
        # What an `ACCOUNT_USAGE` statement raises: one error for every view, or one per view name;
        # `None` means the role may read the views.
        self.usage_outcome = usage_outcome
        # Database name -> its schemas, what `SHOW TERSE ...` lists. `None` makes every name
        # asked for visible, so a test that is not about visibility need not declare any.
        self.databases = databases
        # What `SHOW TERSE DATABASES` / `SHOW TERSE SCHEMAS` raises, keyed by `DATABASES`/`SCHEMAS`.
        self.show_failures = show_failures or {}
        self.closed = False
        self.session_timezone = session_timezone
        self.current_database = "SESSION_DEFAULT_DB"
        self.statements: list[Statement] = []

    def sql(self, sql: str, params: list[Any] | None = None) -> _Collected:
        """Record the statement and answer it."""
        bound = list(params or [])
        self.statements.append(Statement(sql, bound))
        text = sql.strip()
        if text == "USE SECONDARY ROLES ALL":
            if self.fail_on_open is not None:
                raise self.fail_on_open
            return _Collected([])
        if "CURRENT_TIMESTAMP()" in text and text.startswith("SELECT DATE_PART"):
            return _Collected([FakeRow(NOW_MS=NOW_MS)])
        if "CURRENT_ROLE()" in text:
            return _Collected(
                [
                    FakeRow(
                        PRIMARY_ROLE="ROLE_ALPHA",
                        SECONDARY_ROLES='{"roles":"ROLE_BETA","value":"ALL"}',
                    )
                ]
            )
        if ".ACCOUNT_USAGE." in text:
            refusal = self.usage_outcome
            if isinstance(refusal, dict):
                refusal = next(
                    (e for view, e in refusal.items() if f".{view} " in text), None
                )
            if refusal is not None:
                raise refusal
            return _Collected(self._usage_view(text, bound))
        if self.fail_with is not None:
            raise self.fail_with
        if text.startswith("SHOW TERSE "):
            return _Collected(self._visible_names(text))
        if text.startswith("SHOW PARAMETERS LIKE 'event_table' IN "):
            return _Collected(self._event_parameter(text))
        if "FROM IDENTIFIER(?)" in text:
            return _Collected(self._event_table(text, bound))
        if ".TASK_HISTORY(" in text:
            return _Collected(self._task_function(text, bound))
        if ".WAREHOUSE_METERING_HISTORY(" in text:
            return _Collected(self._credit_function(text, bound))
        return _Collected(self._query_function(text, bound))

    def _result_limit(self, text: str) -> int:
        """The `RESULT_LIMIT` of the statement, or the function's default of 100."""
        found = re.search(r"RESULT_LIMIT => (\d+)", text)
        return int(found.group(1)) if found else 100

    def _task_function(self, text: str, bound: list[Any]) -> list[FakeRow]:
        """`TASK_HISTORY`: the name arguments filter before `RESULT_LIMIT`, then `LIMIT` applies."""
        _start, _end, database, schema, name, limit = bound
        kept = [
            r
            for r in self.task_rows
            if str(r["DATABASE_NAME"]).upper() == database
            and str(r["SCHEMA_NAME"]).upper() == schema
            and str(r["NAME"]).upper() == name
        ]
        kept = kept[: self._result_limit(text)]
        return [FakeRow(r) for r in kept[:limit]]

    def _usage_view(self, text: str, bound: list[Any]) -> list[FakeRow]:
        """An `ACCOUNT_USAGE` view: no `RESULT_LIMIT`, the `WHERE` filters, then `LIMIT` applies."""
        if _is_probe(text):
            return [FakeRow(QUERY_ID="probe")]
        params = list(bound)
        limit = params.pop()
        if ".TASK_HISTORY " in text:
            _start, _end, database, schema, name = params
            kept = [
                r
                for r in self.task_rows
                if str(r["DATABASE_NAME"]).upper() == database
                and str(r["SCHEMA_NAME"]).upper() == schema
                and str(r["NAME"]).upper() == name
            ]
        else:
            filters = params[2:]
            kept = list(self.query_rows)
            if "AND WAREHOUSE_NAME = ?" in text:
                name = filters.pop(0)
                kept = [r for r in kept if str(r.get("WAREHOUSE_NAME")).upper() == name]
            if "AND QUERY_ID = ?" in text:
                target = filters.pop(0)
                kept = [r for r in kept if r["QUERY_ID"] == target]
        return [FakeRow(r) for r in kept[:limit]]

    def _credit_function(self, text: str, bound: list[Any]) -> list[FakeRow]:
        """`WAREHOUSE_METERING_HISTORY`: the optional name argument filters, then `LIMIT` applies."""
        params = list(bound)
        limit = params.pop()
        name = params[2] if "WAREHOUSE_NAME => ?" in text else None
        kept = [
            r
            for r in self.credit_rows
            if name is None or str(r["WAREHOUSE_NAME"]).upper() == name
        ]
        return [FakeRow(r) for r in kept[:limit]]

    def _visible_names(self, text: str) -> list[FakeRow]:
        """`SHOW TERSE DATABASES|SCHEMAS [LIKE '<pattern>'] [IN DATABASE <db>]`, `LIKE` wildcards included."""
        kind = text.split()[2]
        if kind in self.show_failures:
            raise self.show_failures[kind]
        like = re.search(r"LIKE '([^']*)'", text)
        if self.databases is None:
            return [FakeRow(name=like.group(1))] if like else []
        if kind == "DATABASES":
            names = list(self.databases)
        else:
            database = text.rsplit(" ", 1)[1]
            names = self.databases.get(database.upper(), [])
        if like:
            pattern = "".join(
                ".*" if c == "%" else "." if c == "_" else re.escape(c)
                for c in like.group(1)
            )
            names = [n for n in names if re.fullmatch(pattern, n, re.IGNORECASE)]
        return [FakeRow(name=n) for n in names]

    def _event_parameter(self, text: str) -> list[FakeRow]:
        """`SHOW PARAMETERS LIKE 'event_table' IN <level>`: no row when the level holds none."""
        level = text.split(" IN ", 1)[1].split()[0]
        value = self.event_tables.get(level)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return []
        return [FakeRow(key="EVENT_TABLE", value=value, level="")]

    def _event_table(self, text: str, bound: list[Any]) -> list[FakeRow]:
        """The event table `SELECT`: the optional query id filters, then `LIMIT` applies."""
        params = list(bound)
        limit = params.pop()
        target = params[3] if "snow.query.id']::STRING = ?" in text else None
        kept = [r for r in self.event_rows if target is None or r["QUERY_ID"] == target]
        return [FakeRow(r) for r in kept[:limit]]

    def _query_function(self, text: str, bound: list[Any]) -> list[FakeRow]:
        """`QUERY_HISTORY*`: `RESULT_LIMIT` applies before the `WHERE` on `QUERY_ID`."""
        params = list(bound)
        warehouse = params.pop(0) if "_BY_WAREHOUSE(" in text else None
        _start, _end = params[0], params[1]
        target = params[2] if "WHERE QUERY_ID = ?" in text else None
        limit = params[-1]
        kept = self.query_rows[: self._result_limit(text)]
        if warehouse is not None:
            kept = [
                r for r in kept if str(r.get("WAREHOUSE_NAME", "")).upper() == warehouse
            ]
        if target is not None:
            kept = [r for r in kept if r["QUERY_ID"] == target]
        return [FakeRow(r) for r in kept[:limit]]

    def close(self) -> None:
        """Record that the session was closed."""
        self.closed = True

    def issued(self) -> list[str]:
        """Every recorded statement's SQL text."""
        return [s.sql for s in self.statements]


class FakeBuilder:
    """A Snowpark `Session.builder` that counts `getOrCreate()` and records `config()`."""

    def __init__(self, session: FakeSession | None = None) -> None:
        """Hold the session the builder will hand out."""
        self.session = session or FakeSession()
        self.configs: list[tuple[str, str]] = []
        self.get_or_create_calls = 0

    def config(self, key: str, value: str) -> FakeBuilder:
        """Record one option and chain."""
        self.configs.append((key, value))
        return self

    def getOrCreate(self) -> FakeSession:  # noqa: N802 — Snowpark's own name
        """Count the call and hand out the session."""
        self.get_or_create_calls += 1
        return self.session


# ── Tool cases: the one list the cross-cutting tests run on ──────────────────────────────────────


@dataclass(frozen=True)
class ToolCase:
    """How the cross-cutting tests call one tool."""

    name: str
    plan: Callable[..., Any]
    run: Callable[..., Any]
    scope: dict[str, Any]
    scope_params: list[str]
    accepts_query_id: bool
    rows_attr: str
    expected_params: list[str]
    stamp: str
    has_text: bool = True
    # A tool that reads `ACCOUNT_USAGE` once the range reaches past the function window.
    account_usage: bool = False
    # What the generic binding test looks for in the data statement; `None` means the scope
    # minus `database`, which is interpolated as a qualifier.
    bound_values: tuple[str, ...] | None = None
    # The two free-text columns the generic text test plants: a long one, then a short one.
    text_fields: tuple[str, str] = ("QUERY_TEXT", "ERROR_MESSAGE")

    def bound(self) -> list[str]:
        """The values that must reach Snowflake as bound parameters, never inside the SQL."""
        if self.bound_values is not None:
            return list(self.bound_values)
        return [v for key, v in self.scope.items() if key != "database"]

    def at_rest(self) -> dict[str, Any]:
        """Every argument of the tool at rest: unset, default limit, no free text."""
        arguments: dict[str, Any] = {name: None for name in self.expected_params}
        arguments["limit"] = 50
        if "include_text" in arguments:
            arguments["include_text"] = False
        return arguments

    def request(self, **overrides: Any) -> Any:
        """Validate a call: this case's scope unless overridden, every other argument at rest."""
        arguments = self.at_rest()
        arguments.update(self.scope)
        arguments.update(overrides)
        return self.plan(**arguments)


TASK_CASE = ToolCase(
    name="task_history",
    plan=tools.plan_task_history,
    run=tools.task_history,
    scope={"database": "MY_DB", "schema": "MY_SCHEMA", "task": "MY_TASK"},
    scope_params=["database", "schema", "task"],
    accepts_query_id=False,
    rows_attr="task_rows",
    expected_params=[
        "database",
        "schema",
        "task",
        "start",
        "end",
        "last_minutes",
        "limit",
        "include_text",
    ],
    stamp="SCHEDULED_TIME",
    account_usage=True,
)
QUERY_CASE = ToolCase(
    name="query_history",
    plan=tools.plan_query_history,
    run=tools.query_history,
    scope={"warehouse": "MY_WH"},
    scope_params=["query_id", "warehouse"],
    accepts_query_id=True,
    rows_attr="query_rows",
    expected_params=[
        "query_id",
        "warehouse",
        "start",
        "end",
        "last_minutes",
        "limit",
        "include_text",
    ],
    stamp="START_TIME",
    account_usage=True,
)
CREDITS_CASE = ToolCase(
    name="warehouse_credits",
    plan=tools.plan_credits,
    run=tools.warehouse_credits,
    scope={"warehouse": "MY_WH"},
    scope_params=["warehouse"],
    accepts_query_id=False,
    rows_attr="credit_rows",
    expected_params=[
        "warehouse",
        "all_warehouses",
        "start",
        "end",
        "last_minutes",
        "limit",
    ],
    stamp="START_TIME",
    has_text=False,
)
EVENT_CASE = ToolCase(
    name="event_table_records",
    plan=tools.plan_event_records,
    run=tools.event_table_records,
    scope={"database": "MY_DB"},
    scope_params=["database", "schema", "query_id"],
    accepts_query_id=False,
    rows_attr="event_rows",
    expected_params=[
        "database",
        "schema",
        "query_id",
        "start",
        "end",
        "last_minutes",
        "limit",
        "include_text",
    ],
    stamp="TIMESTAMP",
    text_fields=("MESSAGE", "ATTRIBUTES"),
    # The default fake holds this `EVENT_TABLE` at the account level.
    bound_values=("SNOWFLAKE.TELEMETRY.EVENTS",),
)
TOOL_CASES = [TASK_CASE, QUERY_CASE, CREDITS_CASE, EVENT_CASE]
CASE_IDS = [case.name for case in TOOL_CASES]


def _is_probe(sql: str) -> bool:
    """Whether a statement is the one-row trial `SELECT` on an `ACCOUNT_USAGE` view."""
    return sql.startswith("SELECT 1 AS PROBE FROM SNOWFLAKE.ACCOUNT_USAGE.")


def _task_row(
    n: int,
    name: str = "MY_TASK",
    schema: str = "MY_SCHEMA",
    **extra: Any,
) -> dict[str, Any]:
    """One `TASK_HISTORY` row."""
    row = {
        "NAME": name,
        "DATABASE_NAME": "MY_DB",
        "SCHEMA_NAME": schema,
        "STATE": "SUCCEEDED",
        "QUERY_ID": f"task-{n}",
        "SCHEDULED_TIME": datetime(2026, 10, 12, 13, 0, 0, tzinfo=UTC),
    }
    row.update(extra)
    return row


def _query_row(
    n: int,
    query_id: str | None = None,
    warehouse: str = "MY_WH",
    **extra: Any,
) -> dict[str, Any]:
    """One `QUERY_HISTORY` row."""
    row = {
        "QUERY_ID": query_id or f"00000000-0000-0000-0000-{n:012d}",
        "WAREHOUSE_NAME": warehouse,
        "EXECUTION_STATUS": "SUCCESS",
        "START_TIME": datetime(2026, 10, 12, 13, 0, 0, tzinfo=UTC),
    }
    row.update(extra)
    return row


def _credit_row(n: int, warehouse: str = "MY_WH", **extra: Any) -> dict[str, Any]:
    """One `WAREHOUSE_METERING_HISTORY` row, `n` hours after the first."""
    row = {
        "WAREHOUSE_NAME": warehouse,
        "START_TIME": datetime(2026, 10, 12, 13, 0, 0, tzinfo=UTC) + timedelta(hours=n),
        "END_TIME": datetime(2026, 10, 12, 14, 0, 0, tzinfo=UTC) + timedelta(hours=n),
        "CREDITS_USED_COMPUTE": Decimal("1.5"),
        "CREDITS_USED_CLOUD_SERVICES": Decimal("0.25"),
        "CREDITS_USED": Decimal("1.75"),
    }
    row.update(extra)
    return row


def _event_row(n: int, query_id: str | None = None, **extra: Any) -> dict[str, Any]:
    """One event table record; `TIMESTAMP` is a naive `TIMESTAMP_NTZ` in UTC, as Snowflake stores it."""
    row = {
        "TIMESTAMP": datetime(2026, 10, 12, 13, 0, 0),
        "RECORD_TYPE": "LOG",
        "SEVERITY": "WARN",
        "QUERY_ID": query_id or f"00000000-0000-0000-0000-{n:012d}",
    }
    row.update(extra)
    return row


def _data_statements(session: FakeSession) -> list[Statement]:
    """The statements that read the history source: not the clock, the roles, a probe nor a `SHOW`."""
    return [
        s
        for s in session.statements
        if re.match(r"\s*SELECT\b", s.sql)
        and "CURRENT_TIMESTAMP()" not in s.sql
        and "CURRENT_ROLE()" not in s.sql
        and not _is_probe(s.sql)
    ]


def _rows_for(case: ToolCase, count: int, **extra: Any) -> list[dict[str, Any]]:
    """`count` history rows of the kind this tool reads."""
    make = {
        "task_history": _task_row,
        "query_history": _query_row,
        "warehouse_credits": _credit_row,
        "event_table_records": _event_row,
    }[case.name]
    return [make(n, **extra) for n in range(count)]


def _ms(iso: str) -> int:
    """Epoch milliseconds of an ISO-8601 instant that carries its offset."""
    return round(datetime.fromisoformat(iso).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No ambient connection settings: an empty `SNOWFLAKE_HOME` and no timezone override."""
    home = tmp_path / "snowflake-home"
    home.mkdir()
    monkeypatch.setenv("SNOWFLAKE_HOME", str(home))
    for name in (
        "SNOWFLAKE_CONNECTION_NAME",
        "SNOWFLAKE_DEFAULT_CONNECTION_NAME",
        "PC_SNOW_TIMEZONE",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


def _write_profiles(home: Path, *names: str, default: str | None = None) -> None:
    """Declare connection profiles in `connections.toml`, and a default in `config.toml`."""
    body = "".join(f'[{name}]\naccount = "{name}-account"\n\n' for name in names)
    (home / "connections.toml").write_text(body, encoding="utf-8")
    if default:
        (home / "config.toml").write_text(
            f'default_connection_name = "{default}"\n', encoding="utf-8"
        )


def run_tool(
    case: ToolCase,
    session: FakeSession | None = None,
    ask: Any = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Validate then run one tool call through a `Connection` backed by a fake builder."""
    builder = FakeBuilder(session)
    connection = tools.Connection(lambda: builder)
    request = case.request(**overrides)
    return asyncio.run(case.run(connection, ask, request))


def run_with_session(
    case: ToolCase, session: FakeSession, **overrides: Any
) -> dict[str, Any]:
    """Run one tool call over the last hour unless the overrides give another range."""
    return run_tool(case, session, **{"last_minutes": 60, **overrides})


# ── Connection ───────────────────────────────────────────────────────────────────────────────────


class TestConnection:
    """The session is opened lazily, once, from the default connection or the user's choice."""

    def test_nothing_is_opened_until_a_tool_runs(self) -> None:
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)
        assert connection.is_open is False
        assert builder.get_or_create_calls == 0

    def test_default_connection_is_opened_once_without_a_profile_name(self) -> None:
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)
        for _ in range(2):
            asyncio.run(
                TASK_CASE.run(connection, None, TASK_CASE.request(last_minutes=5))
            )
        assert builder.get_or_create_calls == 1
        assert builder.configs == []

    def test_the_connection_variable_is_left_to_the_connector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "gamma")
        monkeypatch.setenv("SNOWFLAKE_CONNECTION_NAME", "delta")
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)
        asyncio.run(TASK_CASE.run(connection, None, TASK_CASE.request(last_minutes=5)))
        assert builder.configs == []
        assert builder.get_or_create_calls == 1

    def test_two_profiles_without_default_ask_once_and_remember(
        self, _clean_environment: Path
    ) -> None:
        _write_profiles(_clean_environment, "alpha", "beta")
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)
        asked: list[list[str]] = []

        async def ask(profiles: list[str]) -> str | None:
            asked.append(profiles)
            return "beta"

        async def two_calls() -> None:
            for _ in range(2):
                await TASK_CASE.run(connection, ask, TASK_CASE.request(last_minutes=5))

        asyncio.run(two_calls())
        assert asked == [["alpha", "beta"]]
        assert builder.configs == [("connection_name", "beta")]
        assert builder.get_or_create_calls == 1

    @pytest.mark.parametrize("answer", ["declined", "unavailable", "unknown-profile"])
    def test_refused_or_unavailable_question_names_the_variable_and_opens_nothing(
        self, answer: str, _clean_environment: Path
    ) -> None:
        _write_profiles(_clean_environment, "alpha", "beta")
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)

        async def decline(profiles: list[str]) -> str | None:
            return None

        async def invent(profiles: list[str]) -> str | None:
            return "not-a-profile"

        ask = {"declined": decline, "unavailable": None, "unknown-profile": invent}[
            answer
        ]
        with pytest.raises(
            tools.PcSnowError, match="SNOWFLAKE_DEFAULT_CONNECTION_NAME"
        ):
            asyncio.run(
                TASK_CASE.run(connection, ask, TASK_CASE.request(last_minutes=5))
            )
        assert builder.get_or_create_calls == 0
        assert builder.session.statements == []

    def test_a_default_in_config_toml_needs_no_question(
        self, _clean_environment: Path
    ) -> None:
        _write_profiles(_clean_environment, "alpha", "beta", default="alpha")
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)
        asyncio.run(TASK_CASE.run(connection, None, TASK_CASE.request(last_minutes=5)))
        assert builder.configs == []

    def test_a_default_in_the_environment_needs_no_question(
        self, _clean_environment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_profiles(_clean_environment, "alpha", "beta")
        monkeypatch.setenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "beta")
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)
        asyncio.run(TASK_CASE.run(connection, None, TASK_CASE.request(last_minutes=5)))
        assert builder.configs == []

    def test_a_profile_literally_named_default_needs_no_question(
        self, _clean_environment: Path
    ) -> None:
        _write_profiles(_clean_environment, "default", "beta")
        builder = FakeBuilder()
        connection = tools.Connection(lambda: builder)
        asyncio.run(TASK_CASE.run(connection, None, TASK_CASE.request(last_minutes=5)))
        assert builder.configs == []

    def test_secondary_roles_are_activated_first_and_once(self) -> None:
        session = FakeSession()
        builder = FakeBuilder(session)
        connection = tools.Connection(lambda: builder)

        async def two_calls() -> None:
            for case in (TASK_CASE, QUERY_CASE):
                await case.run(connection, None, case.request(last_minutes=5))

        asyncio.run(two_calls())
        assert session.issued()[0] == "USE SECONDARY ROLES ALL"
        assert session.issued().count("USE SECONDARY ROLES ALL") == 1

    def test_a_failing_opening_statement_is_reported_and_the_session_is_not_kept(
        self,
    ) -> None:
        session = FakeSession(fail_on_open=SqlRefused("role switch refused", 3001))
        builder = FakeBuilder(session)
        connection = tools.Connection(lambda: builder)
        with pytest.raises(tools.PcSnowError) as refused:
            asyncio.run(
                TASK_CASE.run(connection, None, TASK_CASE.request(last_minutes=5))
            )
        message = str(refused.value)
        assert "USE SECONDARY ROLES ALL" in message
        assert "role switch refused" in message
        assert connection.is_open is False
        assert session.closed is True
        assert _data_statements(session) == []

    def test_the_session_timezone_is_never_read_or_set(self) -> None:
        session = FakeSession(session_timezone="Pacific/Auckland")
        for case in TOOL_CASES:
            run_with_session(case, session)
        assert [
            s for s in session.issued() if re.search(r"\bTIMEZONE\b", s.upper())
        ] == []


# ── Scope, range and statements — every tool ─────────────────────────────────────────────────────


@pytest.mark.parametrize("case", TOOL_CASES, ids=CASE_IDS)
class TestContractOfEveryTool:
    """The checks a tool inherits by being listed in `TOOL_CASES`."""

    def test_missing_scope_asks_the_user_and_opens_nothing(
        self, case: ToolCase
    ) -> None:
        builder = FakeBuilder()
        builder.session.current_database = "SESSION_DEFAULT_DB"
        arguments = case.at_rest()
        arguments["last_minutes"] = 60
        with pytest.raises(tools.PcSnowError, match="Ask the user"):
            case.plan(**arguments)
        assert builder.get_or_create_calls == 0
        assert builder.session.statements == []

    def test_missing_range_asks_for_range_and_runs_nothing(
        self, case: ToolCase
    ) -> None:
        with pytest.raises(tools.PcSnowError) as refused:
            case.request()
        message = str(refused.value)
        assert "Ask the user" in message
        assert "last_minutes" in message
        assert ("query_id" in message) is case.accepts_query_id

    def test_last_minutes_excludes_start_and_end(self, case: ToolCase) -> None:
        with pytest.raises(tools.PcSnowError, match="cannot be combined"):
            case.request(last_minutes=10, start="2026-10-12T10:00")

    def test_a_range_over_seven_days_is_refused_before_any_statement(
        self, case: ToolCase
    ) -> None:
        if case.account_usage:
            pytest.skip(
                "reads ACCOUNT_USAGE beyond the window: TestAccountUsageFallback"
            )
        with pytest.raises(tools.PcSnowError, match="7-day"):
            case.request(start="2026-10-01T00:00", end="2026-10-11T00:00")
        with pytest.raises(tools.PcSnowError, match="7-day"):
            case.request(last_minutes=7 * 24 * 60 + 1)

    def test_a_range_that_started_over_seven_days_ago_is_never_sent(
        self, case: ToolCase
    ) -> None:
        if case.account_usage:
            pytest.skip(
                "reads ACCOUNT_USAGE beyond the window: TestAccountUsageFallback"
            )
        session = FakeSession()
        old_start = datetime.fromtimestamp(NOW_MS / 1000, UTC) - timedelta(days=9)
        with pytest.raises(tools.PcSnowError, match="7 days"):
            run_tool(
                case,
                session,
                start=old_start.isoformat(),
                end=(old_start + timedelta(hours=1)).isoformat(),
            )
        assert _data_statements(session) == []

    def test_the_age_limit_of_an_absolute_start_is_the_largest_last_minutes(
        self, case: ToolCase
    ) -> None:
        if case.account_usage:
            pytest.skip(
                "reads ACCOUNT_USAGE beyond the window: TestAccountUsageFallback"
            )
        now = datetime.fromtimestamp(NOW_MS / 1000, UTC)
        stale = now - timedelta(minutes=tools.HISTORY_MINUTES)
        session = FakeSession()
        with pytest.raises(tools.PcSnowError, match="7 days"):
            run_tool(
                case,
                session,
                start=stale.isoformat(),
                end=(stale + timedelta(hours=1)).isoformat(),
            )
        assert _data_statements(session) == []
        newest = stale + timedelta(minutes=1)
        accepted = FakeSession()
        run_tool(
            case,
            accepted,
            start=newest.isoformat(),
            end=(newest + timedelta(hours=1)).isoformat(),
        )
        assert len(_data_statements(accepted)) == 1

    def test_a_range_that_has_not_begun_is_never_sent(self, case: ToolCase) -> None:
        session = FakeSession()
        with pytest.raises(tools.PcSnowError, match="already begun"):
            run_tool(case, session, start="2099-01-01", end="2099-01-02")
        assert _data_statements(session) == []

    @pytest.mark.parametrize(
        ("zone", "value", "reason"),
        [
            ("Europe/Paris", "2026-03-29T02:30:00", "does not exist"),
            ("Europe/Paris", "2026-10-25T02:30:00", "is ambiguous"),
            ("Pacific/Apia", "2011-12-30T12:00:00", "does not exist"),
            ("Europe/Dublin", "2026-10-25T01:30:00", "is ambiguous"),
        ],
    )
    def test_a_local_time_inside_a_daylight_saving_change_is_refused(
        self,
        case: ToolCase,
        monkeypatch: pytest.MonkeyPatch,
        zone: str,
        value: str,
        reason: str,
    ) -> None:
        monkeypatch.setenv("PC_SNOW_TIMEZONE", zone)
        day = value[:10]
        with pytest.raises(tools.PcSnowError, match="explicit offset") as refused:
            case.request(start=value, end=f"{day}T12:00:00+00:00")
        assert value in str(refused.value)
        assert reason in str(refused.value)
        with pytest.raises(tools.PcSnowError, match=reason):
            case.request(start=f"{day}T00:00:00+00:00", end=value)

    def test_a_local_time_outside_a_change_or_with_an_offset_is_accepted(
        self, case: ToolCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PC_SNOW_TIMEZONE", "Europe/Paris")
        outside = case.request(
            start="2026-03-29T03:30:00", end="2026-03-29T12:00:00+00:00"
        ).plan
        assert outside.start_ms == _ms("2026-03-29T01:30:00+00:00")
        explicit = case.request(
            start="2026-10-25T02:30:00+01:00", end="2026-10-25T12:00:00+00:00"
        ).plan
        assert explicit.start_ms == _ms("2026-10-25T01:30:00+00:00")
        before_gap = case.request(
            start="2026-03-29T01:59:00", end="2026-03-29T12:00:00+00:00"
        ).plan
        assert before_gap.start_ms == _ms("2026-03-29T00:59:00+00:00")

    @pytest.mark.skipif(
        not hasattr(time, "tzset"), reason="no time.tzset on this platform"
    )
    def test_a_daylight_saving_gap_is_refused_in_the_system_timezone_too(
        self, case: ToolCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TZ", "Europe/Paris")
        time.tzset()
        try:
            with pytest.raises(tools.PcSnowError, match="does not exist"):
                case.request(start="2026-03-29T02:30:00", end="2026-03-29T12:00:00Z")
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()

    def test_the_largest_last_minutes_is_the_window_of_a_lookup_by_id(
        self, case: ToolCase
    ) -> None:
        largest = tools.MAX_RANGE_MINUTES
        assert case.request(last_minutes=largest).plan.minutes == largest
        if case.account_usage:
            # Beyond the window the tool reads ACCOUNT_USAGE: one minute more is accepted here
            # and bounded by the 365-day retention instead (TestAccountUsageFallback).
            assert case.request(last_minutes=largest + 1).plan.minutes == largest + 1
        else:
            with pytest.raises(tools.PcSnowError, match="7-day"):
                case.request(last_minutes=largest + 1)
        lookup = QUERY_CASE.request(query_id=TARGET_QUERY_ID, warehouse=None)
        assert lookup.plan.minutes == largest

    def test_every_statement_is_a_fixed_select_or_show(self, case: ToolCase) -> None:
        session = FakeSession()
        run_with_session(
            case, session, **({"include_text": True} if case.has_text else {})
        )
        forbidden = re.compile(
            r"\b(USE ROLE|CREATE|ALTER|DROP|INSERT|CALL|UPDATE|DELETE)\b", re.I
        )
        for statement in session.issued()[1:]:
            assert re.match(r"\s*(SELECT|SHOW)\b", statement), statement
            assert not forbidden.search(statement), statement
        assert session.issued()[0] == "USE SECONDARY ROLES ALL"

    def test_a_caller_value_is_refused_or_bound_never_concatenated(
        self, case: ToolCase
    ) -> None:
        hostile = "x'); DROP TABLE t; --"
        for param in case.scope_params:
            with pytest.raises(tools.PcSnowError):
                case.request(**{param: hostile}, last_minutes=5)
        session = FakeSession()
        run_with_session(case, session)
        function = _data_statements(session)[0]
        bound = case.bound()
        assert bound
        for value in bound:
            assert value not in function.sql
            assert value in function.params

    def test_last_minutes_is_read_from_snowflakes_clock(self, case: ToolCase) -> None:
        session = FakeSession()
        before = int(time.time() * 1000)
        run_with_session(case, session, last_minutes=30)
        resolve = session.statements[1]
        function = _data_statements(session)[0]
        assert (
            resolve.sql
            == "SELECT DATE_PART('EPOCH_MILLISECOND', CURRENT_TIMESTAMP()) AS NOW_MS"
        )
        assert NOW_MS - 30 * 60_000 in function.params
        assert NOW_MS in function.params
        assert before not in function.params
        assert not re.search(r"\d{4}-\d{2}-\d{2}", function.sql)

    def test_absolute_dates_are_read_in_the_users_timezone(
        self, case: ToolCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PC_SNOW_TIMEZONE", "Europe/Paris")
        instant = _ms("2026-10-12T13:00:00+00:00")
        for start in (
            "2026-10-12T15:00",
            "2026-10-12T15:00+02:00",
            "2026-10-12T13:00Z",
        ):
            plan = case.request(start=start, end="2026-10-12T16:00:00Z").plan
            assert plan.start_ms == instant

    @pytest.mark.skipif(
        not hasattr(time, "tzset"), reason="no time.tzset on this platform"
    )
    def test_system_timezone_is_the_fallback_and_the_override_wins(
        self, case: ToolCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            absent = case.request(
                start="2026-10-12T15:00", end="2026-10-12T16:00Z"
            ).plan
            assert absent.start_ms == _ms("2026-10-12T06:00:00+00:00")
            monkeypatch.setenv("PC_SNOW_TIMEZONE", "Europe/Paris")
            present = case.request(
                start="2026-10-12T15:00", end="2026-10-12T16:00Z"
            ).plan
            assert present.start_ms == _ms("2026-10-12T13:00:00+00:00")
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()

    def test_invalid_timezone_name_is_refused(
        self, case: ToolCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PC_SNOW_TIMEZONE", "Mars/Olympus")
        with pytest.raises(tools.PcSnowError, match="PC_SNOW_TIMEZONE"):
            case.request(last_minutes=5)

    def test_timestamps_are_rendered_in_the_users_zone_with_their_offset(
        self, case: ToolCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PC_SNOW_TIMEZONE", "Europe/Paris")
        session = FakeSession(**{case.rows_attr: _rows_for(case, 1)})
        row = run_with_session(case, session)["rows"][0]
        assert row[case.stamp] == "2026-10-12T15:00:00.000+02:00"

    def test_resolved_range_is_returned_with_its_offset(
        self, case: ToolCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PC_SNOW_TIMEZONE", "Europe/Paris")
        relative = run_with_session(case, FakeSession())["range"]
        assert relative == {
            "start": "2001-09-09T02:46:40.000+02:00",
            "end": "2001-09-09T03:46:40.000+02:00",
            "timezone": "Europe/Paris",
        }
        absolute = run_tool(
            case,
            FakeSession(),
            start="2001-09-09T03:00",
            end="2001-09-09T03:30",
        )["range"]
        assert absolute == {
            "start": "2001-09-09T03:00:00.000+02:00",
            "end": "2001-09-09T03:30:00.000+02:00",
            "timezone": "Europe/Paris",
        }

    def test_rows_are_capped_at_fifty_by_default_and_flagged(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(**{case.rows_attr: _rows_for(case, 300)})
        result = run_with_session(case, session)
        assert result["row_count"] == 50
        assert result["truncated"] is True

    def test_two_hundred_rows_can_be_asked_for_and_more_cannot(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(**{case.rows_attr: _rows_for(case, 300)})
        result = run_with_session(case, session, limit=200)
        assert result["row_count"] == 200
        assert result["truncated"] is True
        with pytest.raises(tools.PcSnowError, match="200"):
            case.request(last_minutes=5, limit=201)

    def test_a_short_history_is_not_flagged(self, case: ToolCase) -> None:
        session = FakeSession(**{case.rows_attr: _rows_for(case, 3)})
        result = run_with_session(case, session)
        assert result["row_count"] == 3
        assert result["truncated"] is False

    def test_result_names_its_source_and_freshness(self, case: ToolCase) -> None:
        result = run_with_session(case, FakeSession())
        assert result["source"].strip() != ""
        assert result["fraicheur"].strip() != ""

    def test_free_text_is_withheld_unless_asked_for_and_cut_when_given(
        self, case: ToolCase
    ) -> None:
        if not case.has_text:
            pytest.skip("this tool returns no free text")
        long_field, short_field = case.text_fields
        text = {long_field: "x" * 500, short_field: "boom"}
        session = FakeSession(**{case.rows_attr: _rows_for(case, 1, **text)})
        default = run_with_session(case, session)
        assert long_field not in default["rows"][0]
        assert short_field not in default["rows"][0]
        assert long_field not in session.statements[-1].sql
        asked = run_with_session(case, session, include_text=True)
        assert asked["rows"][0][long_field] == "x" * 200
        assert asked["rows"][0][short_field] == "boom"
        assert asked["truncated_fields"] == [
            {"row": 0, "field": long_field, "original_length": 500}
        ]

    def test_privilege_refusal_carries_the_message_the_privilege_and_the_roles(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(fail_with=SqlRefused(NO_PRIVILEGE, 3001))
        with pytest.raises(tools.PcSnowError) as refused:
            run_with_session(case, session)
        message = str(refused.value)
        assert NO_PRIVILEGE in message
        assert "Missing privilege:" in message
        assert "CURRENT_ROLE()=ROLE_ALPHA" in message
        assert (
            'CURRENT_SECONDARY_ROLES()={"roles":"ROLE_BETA","value":"ALL"}' in message
        )

    def test_any_other_snowflake_failure_is_reported_with_its_message(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(fail_with=SqlRefused("invalid identifier 'X'", 904))
        with pytest.raises(tools.PcSnowError, match="invalid identifier 'X'"):
            run_with_session(case, session)


# ── ACCOUNT_USAGE fallback — task_history and query_history ──────────────────────────────────────

FALLBACK_CASES = [TASK_CASE, QUERY_CASE]
FALLBACK_IDS = [case.name for case in FALLBACK_CASES]
TEN_DAYS_MINUTES = 10 * 24 * 60
NO_USAGE = SqlRefused(USAGE_REFUSED, 2003)


def _probes(session: FakeSession, view: str | None = None) -> list[Statement]:
    """The trial `SELECT` statements the session received, for one view or all."""
    return [
        s
        for s in session.statements
        if _is_probe(s.sql) and (view is None or f".{view} " in s.sql)
    ]


def _usage_reads(session: FakeSession) -> list[Statement]:
    """The statements that read an `ACCOUNT_USAGE` view for data, the probe excluded."""
    return [
        s
        for s in session.statements
        if ".ACCOUNT_USAGE." in s.sql and not _is_probe(s.sql)
    ]


@pytest.mark.parametrize("case", FALLBACK_CASES, ids=FALLBACK_IDS)
class TestAccountUsageFallback:
    """Past the function window the view is read, once the access to it is known."""

    def test_a_ten_day_range_reads_the_account_usage_view_and_says_it_is_delayed(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(**{case.rows_attr: _rows_for(case, 2)})
        result = run_tool(case, session, last_minutes=TEN_DAYS_MINUTES)
        reads = _usage_reads(session)
        assert len(reads) == 1
        assert result["source"] == f"SNOWFLAKE.ACCOUNT_USAGE.{case.name.upper()}"
        assert "différée" in result["fraicheur"]
        assert result["row_count"] == 2
        assert NOW_MS - TEN_DAYS_MINUTES * 60_000 in reads[0].params
        assert not any("INFORMATION_SCHEMA" in s.sql for s in session.statements)

    def test_a_ten_day_absolute_range_falls_back_the_same_way(
        self, case: ToolCase
    ) -> None:
        end = datetime.fromtimestamp(NOW_MS / 1000, UTC) - timedelta(days=1)
        start = end - timedelta(days=10)
        session = FakeSession()
        result = run_tool(case, session, start=start.isoformat(), end=end.isoformat())
        assert result["source"].startswith("SNOWFLAKE.ACCOUNT_USAGE.")
        assert _usage_reads(session)[0].params[:2] == [
            round(start.timestamp() * 1000),
            round(end.timestamp() * 1000),
        ]

    def test_a_short_range_that_started_over_seven_days_ago_falls_back(
        self, case: ToolCase
    ) -> None:
        start = datetime.fromtimestamp(NOW_MS / 1000, UTC) - timedelta(days=9)
        session = FakeSession()
        result = run_tool(
            case,
            session,
            start=start.isoformat(),
            end=(start + timedelta(hours=1)).isoformat(),
        )
        assert result["source"].startswith("SNOWFLAKE.ACCOUNT_USAGE.")

    def test_the_function_window_is_the_edge_of_the_fallback(
        self, case: ToolCase
    ) -> None:
        now = datetime.fromtimestamp(NOW_MS / 1000, UTC)
        inside = now - timedelta(minutes=tools.MAX_RANGE_MINUTES)
        session = FakeSession()
        result = run_tool(
            case,
            session,
            start=inside.isoformat(),
            end=(inside + timedelta(hours=1)).isoformat(),
        )
        assert result["source"].count("INFORMATION_SCHEMA") == 1
        assert _probes(session) == []
        assert _usage_reads(session) == []

    def test_a_range_within_the_window_never_probes(self, case: ToolCase) -> None:
        session = FakeSession(usage_outcome=NO_USAGE)
        result = run_with_session(case, session)
        assert "INFORMATION_SCHEMA" in result["source"]
        assert _probes(session) == []

    def test_the_view_retention_bounds_the_range(self, case: ToolCase) -> None:
        with pytest.raises(tools.PcSnowError, match="365-day"):
            case.request(last_minutes=tools.ACCOUNT_USAGE_MINUTES + 1)
        with pytest.raises(tools.PcSnowError, match="365-day"):
            case.request(start="2024-01-01T00:00", end="2025-06-01T00:00")
        assert (
            case.request(last_minutes=tools.ACCOUNT_USAGE_MINUTES).plan.minutes
            == tools.ACCOUNT_USAGE_MINUTES
        )
        session = FakeSession()
        old = datetime.fromtimestamp(NOW_MS / 1000, UTC) - timedelta(days=400)
        with pytest.raises(tools.PcSnowError, match="365 days"):
            run_tool(
                case,
                session,
                start=old.isoformat(),
                end=(old + timedelta(hours=1)).isoformat(),
            )
        assert _probes(session) == []

    def test_two_fallback_calls_probe_once_and_a_new_process_probes_again(
        self, case: ToolCase
    ) -> None:
        view = USAGE_VIEW[case.name]
        session = FakeSession()
        connection = tools.Connection(lambda: FakeBuilder(session))
        for _ in range(2):
            asyncio.run(
                case.run(connection, None, case.request(last_minutes=TEN_DAYS_MINUTES))
            )
        assert len(_probes(session, view)) == 1
        assert len(_probes(session)) == 1
        assert len(_usage_reads(session)) == 2
        restarted = tools.Connection(lambda: FakeBuilder(session))
        asyncio.run(
            case.run(restarted, None, case.request(last_minutes=TEN_DAYS_MINUTES))
        )
        assert len(_probes(session, view)) == 2

    def test_the_probe_is_a_bounded_select_and_nothing_is_written(
        self, case: ToolCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        before = {p for p in tmp_path.rglob("*")}
        home = Path(os.environ["SNOWFLAKE_HOME"])
        home_before = {p for p in home.rglob("*")}
        session = FakeSession()
        run_tool(case, session, last_minutes=TEN_DAYS_MINUTES)
        assert [s.sql for s in _probes(session)] == [
            USAGE_PROBE.format(view=USAGE_VIEW[case.name])
        ]
        assert {p for p in tmp_path.rglob("*")} == before
        assert {p for p in home.rglob("*")} == home_before

    def test_a_refused_probe_names_the_role_of_its_view_only(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(usage_outcome=NO_USAGE)
        with pytest.raises(tools.PcSnowError) as refused:
            run_tool(case, session, last_minutes=TEN_DAYS_MINUTES)
        message = str(refused.value)
        own = USAGE_ROLE[case.name]
        other = next(r for name, r in USAGE_ROLE.items() if name != case.name)
        assert f"GRANT DATABASE ROLE {own} TO ROLE" in message
        assert other not in message
        assert "IMPORTED PRIVILEGES" not in message
        assert USAGE_REFUSED in message
        assert "CURRENT_ROLE()=ROLE_ALPHA" in message
        assert _usage_reads(session) == []

    def test_a_denial_is_remembered_without_asking_snowflake_again(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(usage_outcome=NO_USAGE)
        connection = tools.Connection(lambda: FakeBuilder(session))
        for _ in range(2):
            with pytest.raises(tools.PcSnowError, match=USAGE_ROLE[case.name]):
                asyncio.run(
                    case.run(
                        connection, None, case.request(last_minutes=TEN_DAYS_MINUTES)
                    )
                )
        assert len(_probes(session)) == 1

    def test_a_failure_that_is_not_a_refusal_is_not_remembered(
        self, case: ToolCase
    ) -> None:
        session = FakeSession(usage_outcome=SqlRefused("network timeout", 604))
        connection = tools.Connection(lambda: FakeBuilder(session))
        with pytest.raises(tools.PcSnowError, match="network timeout"):
            asyncio.run(
                case.run(connection, None, case.request(last_minutes=TEN_DAYS_MINUTES))
            )
        session.usage_outcome = None
        result = asyncio.run(
            case.run(connection, None, case.request(last_minutes=TEN_DAYS_MINUTES))
        )
        assert result["source"].startswith("SNOWFLAKE.ACCOUNT_USAGE.")
        assert len(_probes(session)) == 2

    def test_a_view_refused_after_a_good_probe_still_names_its_own_role(
        self, case: ToolCase
    ) -> None:
        class ProbeOnly(FakeSession):
            def _usage_view(self, text: str, bound: list[Any]) -> list[FakeRow]:
                if not _is_probe(text):
                    raise NO_USAGE
                return super()._usage_view(text, bound)

        session = ProbeOnly()
        with pytest.raises(tools.PcSnowError, match=USAGE_ROLE[case.name]):
            run_tool(case, session, last_minutes=TEN_DAYS_MINUTES)

    def test_the_result_note_names_the_role_of_the_view_it_read(
        self, case: ToolCase
    ) -> None:
        result = run_tool(case, FakeSession(), last_minutes=TEN_DAYS_MINUTES)
        assert [USAGE_ROLE[case.name] in note for note in result["notes"]] == [True]

    def test_free_text_is_withheld_unless_asked_for_on_the_view_too(
        self, case: ToolCase
    ) -> None:
        long_field, short_field = case.text_fields
        text = {long_field: "x" * 500, short_field: "boom"}
        session = FakeSession(**{case.rows_attr: _rows_for(case, 1, **text)})
        default = run_tool(case, session, last_minutes=TEN_DAYS_MINUTES)
        assert long_field not in default["rows"][0]
        assert long_field not in _usage_reads(session)[0].sql
        asked = run_tool(
            case, session, last_minutes=TEN_DAYS_MINUTES, include_text=True
        )
        assert asked["rows"][0][long_field] == "x" * 200
        assert asked["rows"][0][short_field] == "boom"

    def test_rows_are_capped_and_flagged_on_the_view_too(self, case: ToolCase) -> None:
        session = FakeSession(**{case.rows_attr: _rows_for(case, 300)})
        result = run_tool(case, session, last_minutes=TEN_DAYS_MINUTES)
        assert result["row_count"] == 50
        assert result["truncated"] is True
        assert _usage_reads(session)[0].params[-1] == 51

    def test_every_view_statement_is_a_fixed_select_with_bound_values(
        self, case: ToolCase
    ) -> None:
        session = FakeSession()
        run_tool(case, session, last_minutes=TEN_DAYS_MINUTES)
        for statement in _probes(session) + _usage_reads(session):
            assert re.match(r"\s*SELECT\b", statement.sql)
            assert not re.search(r"\d{4}-\d{2}-\d{2}", statement.sql)
        for value in case.scope.values():
            assert all(value not in s.sql for s in _usage_reads(session))


class TestAccessIsMeasuredPerView:
    """The role that covers one view says nothing about the other."""

    def test_each_view_is_probed_once_per_process_in_turn(self) -> None:
        session = FakeSession()
        connection = tools.Connection(lambda: FakeBuilder(session))
        for case in (TASK_CASE, TASK_CASE, QUERY_CASE, QUERY_CASE):
            asyncio.run(
                case.run(connection, None, case.request(last_minutes=TEN_DAYS_MINUTES))
            )
        assert [s.sql for s in _probes(session)] == [
            USAGE_PROBE.format(view="TASK_HISTORY"),
            USAGE_PROBE.format(view="QUERY_HISTORY"),
        ]

    def test_task_history_still_reads_when_only_query_history_is_refused(self) -> None:
        session = FakeSession(
            task_rows=[_task_row(0)], usage_outcome={"QUERY_HISTORY": NO_USAGE}
        )
        connection = tools.Connection(lambda: FakeBuilder(session))
        with pytest.raises(tools.PcSnowError) as refused:
            asyncio.run(
                QUERY_CASE.run(
                    connection, None, QUERY_CASE.request(last_minutes=TEN_DAYS_MINUTES)
                )
            )
        assert "SNOWFLAKE.GOVERNANCE_VIEWER" in str(refused.value)
        assert "SNOWFLAKE.USAGE_VIEWER" not in str(refused.value)
        result = asyncio.run(
            TASK_CASE.run(
                connection, None, TASK_CASE.request(last_minutes=TEN_DAYS_MINUTES)
            )
        )
        assert result["source"] == "SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY"
        assert result["row_count"] == 1

    def test_query_history_still_reads_when_only_task_history_is_refused(self) -> None:
        session = FakeSession(usage_outcome={"TASK_HISTORY": NO_USAGE})
        result = run_tool(QUERY_CASE, session, last_minutes=TEN_DAYS_MINUTES)
        assert result["source"] == "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY"
        with pytest.raises(tools.PcSnowError, match="SNOWFLAKE.USAGE_VIEWER"):
            run_tool(TASK_CASE, session, last_minutes=TEN_DAYS_MINUTES)


class TestUsageAccessIsNotPoisoned:
    """A denial is remembered only when Snowflake's own error code says so."""

    def test_a_refusal_known_by_its_text_alone_is_raised_and_not_remembered(
        self,
    ) -> None:
        proxy = Exception("403 Forbidden: not authorized by the identity provider")
        session = FakeSession(usage_outcome=proxy)
        connection = tools.Connection(lambda: FakeBuilder(session))
        for _ in range(2):
            with pytest.raises(tools.PcSnowError, match="SNOWFLAKE.USAGE_VIEWER"):
                asyncio.run(
                    TASK_CASE.run(
                        connection,
                        None,
                        TASK_CASE.request(last_minutes=TEN_DAYS_MINUTES),
                    )
                )
        assert len(_probes(session)) == 2
        session.usage_outcome = None
        result = asyncio.run(
            TASK_CASE.run(
                connection, None, TASK_CASE.request(last_minutes=TEN_DAYS_MINUTES)
            )
        )
        assert result["source"] == "SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY"

    def test_a_view_the_server_does_not_read_is_refused_before_any_statement(
        self,
    ) -> None:
        session = FakeSession()
        with pytest.raises(tools.PcSnowError, match="not an ACCOUNT_USAGE view"):
            tools.UsageAccess().require(
                tools.guarded_runner(session), "X; DROP TABLE t"
            )
        assert session.statements == []

    def test_concurrent_calls_share_one_probe_per_view(self) -> None:
        class SlowProbe(FakeSession):
            def sql(self, sql: str, params: list[Any] | None = None) -> Any:
                if _is_probe(sql):
                    time.sleep(0.05)
                return super().sql(sql, params)

        session = SlowProbe()
        usage = tools.UsageAccess()
        run = tools.guarded_runner(session)
        barrier = threading.Barrier(16)
        errors: list[BaseException] = []

        def call(view: str) -> None:
            try:
                barrier.wait()
                usage.require(run, view)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=call, args=(view,))
            for view in ("TASK_HISTORY", "QUERY_HISTORY")
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert sorted(s.sql for s in _probes(session)) == [
            USAGE_PROBE.format(view="QUERY_HISTORY"),
            USAGE_PROBE.format(view="TASK_HISTORY"),
        ]

    def test_concurrent_tool_calls_on_one_shared_usage_probe_once(self) -> None:
        session = FakeSession()
        usage = tools.UsageAccess()
        request = TASK_CASE.request(last_minutes=TEN_DAYS_MINUTES)
        with ThreadPoolExecutor(8) as pool:
            results = list(
                pool.map(
                    lambda _: tools.task_history_on(session, request, usage), range(8)
                )
            )
        assert len(results) == 8
        assert len(_probes(session)) == 1


class TestAccountUsageTasks:
    """What only the task fallback promises."""

    def test_only_the_named_task_is_read_with_its_scope_bound(self) -> None:
        others = [_task_row(n, name="NOISY_TASK") for n in range(5)]
        wanted = [_task_row(0, name="MY_TASK", QUERY_ID="mine-0")]
        session = FakeSession(task_rows=others + wanted)
        result = run_tool(TASK_CASE, session, last_minutes=TEN_DAYS_MINUTES)
        assert [r["QUERY_ID"] for r in result["rows"]] == ["mine-0"]
        assert _usage_reads(session)[0].params == [
            NOW_MS - TEN_DAYS_MINUTES * 60_000,
            NOW_MS,
            "MY_DB",
            "MY_SCHEMA",
            "MY_TASK",
            51,
        ]

    def test_task_view_statement_text(self) -> None:
        assert tools.build_account_usage_task_history_sql(include_text=False) == (
            "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, STATE, ERROR_CODE, SCHEDULED_TIME, "
            "QUERY_START_TIME, COMPLETED_TIME, SCHEDULED_FROM, QUERY_ID, GRAPH_RUN_GROUP_ID "
            "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
            "WHERE SCHEDULED_TIME >= TO_TIMESTAMP_LTZ(?, 3) "
            "AND SCHEDULED_TIME < TO_TIMESTAMP_LTZ(?, 3) "
            "AND DATABASE_NAME = ? AND SCHEMA_NAME = ? AND NAME = ? "
            "ORDER BY SCHEDULED_TIME DESC LIMIT ?"
        )

    def test_task_view_statement_text_with_free_text(self) -> None:
        assert tools.build_account_usage_task_history_sql(include_text=True) == (
            "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, STATE, ERROR_CODE, SCHEDULED_TIME, "
            "QUERY_START_TIME, COMPLETED_TIME, SCHEDULED_FROM, QUERY_ID, GRAPH_RUN_GROUP_ID, "
            "ERROR_MESSAGE, RETURN_VALUE, QUERY_TEXT "
            "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
            "WHERE SCHEDULED_TIME >= TO_TIMESTAMP_LTZ(?, 3) "
            "AND SCHEDULED_TIME < TO_TIMESTAMP_LTZ(?, 3) "
            "AND DATABASE_NAME = ? AND SCHEMA_NAME = ? AND NAME = ? "
            "ORDER BY SCHEDULED_TIME DESC LIMIT ?"
        )


class TestAccountUsageQueries:
    """What only the query fallback promises."""

    def test_a_warehouse_and_a_query_id_filter_the_view_with_bound_values(self) -> None:
        rows = [
            _query_row(1, warehouse="MY_WH"),
            _query_row(2, warehouse="OTHER_WH"),
            _query_row(3, query_id=TARGET_QUERY_ID, warehouse="MY_WH"),
        ]
        session = FakeSession(query_rows=rows)
        by_warehouse = run_tool(QUERY_CASE, session, last_minutes=TEN_DAYS_MINUTES)
        assert [r["WAREHOUSE_NAME"] for r in by_warehouse["rows"]] == ["MY_WH", "MY_WH"]
        assert _usage_reads(session)[0].params == [
            NOW_MS - TEN_DAYS_MINUTES * 60_000,
            NOW_MS,
            "MY_WH",
            51,
        ]
        by_id = run_tool(
            QUERY_CASE,
            session,
            last_minutes=TEN_DAYS_MINUTES,
            query_id=TARGET_QUERY_ID,
        )
        assert [r["QUERY_ID"] for r in by_id["rows"]] == [TARGET_QUERY_ID]

    def test_a_query_id_alone_still_uses_the_function_window(self) -> None:
        session = FakeSession(usage_outcome=NO_USAGE)
        result = run_tool(QUERY_CASE, session, query_id=TARGET_QUERY_ID, warehouse=None)
        assert result["source"] == "SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY"
        assert _probes(session) == []

    def test_query_view_statement_text(self) -> None:
        assert tools.build_account_usage_query_history_sql(
            by_warehouse=True, by_query_id=True, include_text=False
        ) == (
            "SELECT QUERY_ID, USER_NAME, ROLE_NAME, WAREHOUSE_NAME, WAREHOUSE_SIZE, QUERY_TYPE, "
            "EXECUTION_STATUS, ERROR_CODE, START_TIME, END_TIME, TOTAL_ELAPSED_TIME, "
            "QUEUED_OVERLOAD_TIME, BYTES_SCANNED, ROWS_PRODUCED, QUERY_TAG "
            "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY "
            "WHERE END_TIME >= TO_TIMESTAMP_LTZ(?, 3) "
            "AND END_TIME < TO_TIMESTAMP_LTZ(?, 3) "
            "AND WAREHOUSE_NAME = ? AND QUERY_ID = ? "
            "ORDER BY START_TIME DESC LIMIT ?"
        )

    def test_plain_query_view_statement_text_with_free_text(self) -> None:
        assert tools.build_account_usage_query_history_sql(
            by_warehouse=False, by_query_id=False, include_text=True
        ) == (
            "SELECT QUERY_ID, USER_NAME, ROLE_NAME, WAREHOUSE_NAME, WAREHOUSE_SIZE, QUERY_TYPE, "
            "EXECUTION_STATUS, ERROR_CODE, START_TIME, END_TIME, TOTAL_ELAPSED_TIME, "
            "QUEUED_OVERLOAD_TIME, BYTES_SCANNED, ROWS_PRODUCED, QUERY_TAG, "
            "ERROR_MESSAGE, QUERY_TEXT "
            "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY "
            "WHERE END_TIME >= TO_TIMESTAMP_LTZ(?, 3) "
            "AND END_TIME < TO_TIMESTAMP_LTZ(?, 3) "
            "ORDER BY START_TIME DESC LIMIT ?"
        )


class TestToolsWithoutFallback:
    """Credits and event records keep the function window as their limit."""

    @pytest.mark.parametrize(
        "case", [CREDITS_CASE, EVENT_CASE], ids=["credits", "event"]
    )
    def test_a_ten_day_range_is_still_refused(self, case: ToolCase) -> None:
        with pytest.raises(tools.PcSnowError, match="7-day"):
            case.request(last_minutes=TEN_DAYS_MINUTES)


# ── Tool-specific behaviour ──────────────────────────────────────────────────────────────────────


class TestTaskHistory:
    """What only the task tool promises."""

    def test_only_the_named_task_comes_back_among_many_others(self) -> None:
        others = [_task_row(n, name="NOISY_TASK") for n in range(12_000)]
        wanted = [_task_row(n, name="MY_TASK", QUERY_ID=f"mine-{n}") for n in range(3)]
        session = FakeSession(task_rows=others + wanted)
        result = run_with_session(TASK_CASE, session)
        assert [r["QUERY_ID"] for r in result["rows"]] == ["mine-0", "mine-1", "mine-2"]
        function = session.statements[-1]
        assert function.params == [
            NOW_MS - 60 * 60_000,
            NOW_MS,
            "MY_DB",
            "MY_SCHEMA",
            "MY_TASK",
            51,
        ]
        assert "MY_DB.INFORMATION_SCHEMA.TASK_HISTORY(" in function.sql

    def test_task_statement_text(self) -> None:
        assert tools.build_task_history_sql("MY_DB", include_text=False) == (
            "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, STATE, ERROR_CODE, SCHEDULED_TIME, "
            "QUERY_START_TIME, COMPLETED_TIME, SCHEDULED_FROM, QUERY_ID, GRAPH_RUN_GROUP_ID "
            "FROM TABLE(MY_DB.INFORMATION_SCHEMA.TASK_HISTORY("
            "SCHEDULED_TIME_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
            "SCHEDULED_TIME_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
            "RESULT_LIMIT => 10000, "
            "DATABASE_NAME => ?::STRING, "
            "SCHEMA_NAME => ?::STRING, "
            "TASK_NAME => ?::STRING)) "
            "ORDER BY SCHEDULED_TIME DESC LIMIT ?"
        )

    def test_task_statement_text_with_free_text(self) -> None:
        assert tools.build_task_history_sql("MY_DB", include_text=True) == (
            "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, STATE, ERROR_CODE, SCHEDULED_TIME, "
            "QUERY_START_TIME, COMPLETED_TIME, SCHEDULED_FROM, QUERY_ID, GRAPH_RUN_GROUP_ID, "
            "ERROR_MESSAGE, RETURN_VALUE, QUERY_TEXT "
            "FROM TABLE(MY_DB.INFORMATION_SCHEMA.TASK_HISTORY("
            "SCHEDULED_TIME_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
            "SCHEDULED_TIME_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
            "RESULT_LIMIT => 10000, "
            "DATABASE_NAME => ?::STRING, "
            "SCHEMA_NAME => ?::STRING, "
            "TASK_NAME => ?::STRING)) "
            "ORDER BY SCHEDULED_TIME DESC LIMIT ?"
        )


class TestQueryHistory:
    """What only the query tool promises."""

    def test_a_query_id_alone_finds_a_query_beyond_the_default_hundred(self) -> None:
        rows = _rows_for(QUERY_CASE, 150)
        rows[119] = _query_row(119, query_id=TARGET_QUERY_ID)
        session = FakeSession(query_rows=rows)
        result = run_tool(QUERY_CASE, session, query_id=TARGET_QUERY_ID, warehouse=None)
        assert [r["QUERY_ID"] for r in result["rows"]] == [TARGET_QUERY_ID]
        assert "RESULT_LIMIT => 10000" in session.statements[-1].sql

    def test_a_query_id_alone_searches_the_whole_window_by_snowflakes_clock(
        self,
    ) -> None:
        session = FakeSession()
        result = run_tool(QUERY_CASE, session, query_id=TARGET_QUERY_ID, warehouse=None)
        function = session.statements[-1]
        assert function.params == [
            NOW_MS - (7 * 24 * 60 - 1) * 60_000,
            NOW_MS,
            TARGET_QUERY_ID,
            51,
        ]
        assert re.search(r"[+-]\d\d:\d\d$", result["range"]["end"])

    def test_by_warehouse_the_function_is_the_warehouse_variant(self) -> None:
        session = FakeSession(
            query_rows=[
                _query_row(1, warehouse="MY_WH"),
                _query_row(2, warehouse="OTHER_WH"),
            ]
        )
        result = run_with_session(QUERY_CASE, session)
        assert [r["WAREHOUSE_NAME"] for r in result["rows"]] == ["MY_WH"]
        assert (
            result["source"]
            == "SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE"
        )
        assert session.statements[-1].params[0] == "MY_WH"

    def test_without_warehouse_the_result_says_whose_queries_it_covers(self) -> None:
        result = run_tool(
            QUERY_CASE, FakeSession(), query_id=TARGET_QUERY_ID, warehouse=None
        )
        assert result["source"] == "SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY"
        assert result["notes"] == [
            "QUERY_HISTORY returns the connected user's queries, and other users' only "
            "when the active role holds MONITOR or OPERATE on their warehouse or task, "
            "or MONITOR EXECUTION; pass a warehouse to restrict to it."
        ]

    def test_a_malformed_query_id_is_refused(self) -> None:
        with pytest.raises(tools.PcSnowError, match="query id"):
            QUERY_CASE.request(query_id="1; DROP TABLE t", warehouse=None)

    def test_query_statement_text(self) -> None:
        assert tools.build_query_history_sql(
            by_warehouse=True, by_query_id=True, include_text=False
        ) == (
            "SELECT QUERY_ID, USER_NAME, ROLE_NAME, WAREHOUSE_NAME, WAREHOUSE_SIZE, QUERY_TYPE, "
            "EXECUTION_STATUS, ERROR_CODE, START_TIME, END_TIME, TOTAL_ELAPSED_TIME, "
            "QUEUED_OVERLOAD_TIME, BYTES_SCANNED, ROWS_PRODUCED, QUERY_TAG "
            "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE("
            "WAREHOUSE_NAME => ?::STRING, "
            "END_TIME_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
            "END_TIME_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
            "RESULT_LIMIT => 10000)) "
            "WHERE QUERY_ID = ? "
            "ORDER BY START_TIME DESC LIMIT ?"
        )

    def test_plain_query_statement_text_with_free_text(self) -> None:
        assert tools.build_query_history_sql(
            by_warehouse=False, by_query_id=False, include_text=True
        ) == (
            "SELECT QUERY_ID, USER_NAME, ROLE_NAME, WAREHOUSE_NAME, WAREHOUSE_SIZE, QUERY_TYPE, "
            "EXECUTION_STATUS, ERROR_CODE, START_TIME, END_TIME, TOTAL_ELAPSED_TIME, "
            "QUEUED_OVERLOAD_TIME, BYTES_SCANNED, ROWS_PRODUCED, QUERY_TAG, "
            "ERROR_MESSAGE, QUERY_TEXT "
            "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY("
            "END_TIME_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
            "END_TIME_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
            "RESULT_LIMIT => 10000)) "
            "ORDER BY START_TIME DESC LIMIT ?"
        )


class TestWarehouseCredits:
    """What only the credits tool promises."""

    def test_three_hourly_rows_carry_warehouse_hour_and_both_credit_kinds_in_the_users_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PC_SNOW_TIMEZONE", "Europe/Paris")
        rows = [
            _credit_row(
                n,
                CREDITS_USED_COMPUTE=Decimal(str(n + 1)),
                CREDITS_USED_CLOUD_SERVICES=Decimal("0.5"),
            )
            for n in range(3)
        ]
        session = FakeSession(credit_rows=rows)
        result = run_with_session(CREDITS_CASE, session)
        assert result["source"] == (
            "SNOWFLAKE.INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY"
        )
        assert result["timezone"] == "Europe/Paris"
        assert result["rows"] == [
            {
                "WAREHOUSE_NAME": "MY_WH",
                "START_TIME": f"2026-10-12T{15 + n}:00:00.000+02:00",
                "END_TIME": f"2026-10-12T{16 + n}:00:00.000+02:00",
                "CREDITS_USED_COMPUTE": n + 1,
                "CREDITS_USED_CLOUD_SERVICES": 0.5,
                "CREDITS_USED": 1.75,
            }
            for n in range(3)
        ]

    def test_no_privilege_names_monitor_usage(self) -> None:
        session = FakeSession(
            fail_with=SqlRefused("Insufficient privileges to operate on function", 3001)
        )
        with pytest.raises(tools.PcSnowError, match="MONITOR USAGE"):
            run_with_session(CREDITS_CASE, session)

    def test_without_warehouse_or_flag_the_user_is_asked_and_nothing_is_sent(
        self,
    ) -> None:
        session = FakeSession()
        with pytest.raises(tools.PcSnowError) as refused:
            run_tool(CREDITS_CASE, session, warehouse=None, last_minutes=60)
        message = str(refused.value)
        assert (
            "Ask the user which warehouse, or whether they want all of them" in message
        )
        assert session.statements == []

    def test_a_warehouse_and_the_flag_together_are_ambiguous(self) -> None:
        with pytest.raises(tools.PcSnowError, match="not both"):
            CREDITS_CASE.request(all_warehouses=True, last_minutes=60)

    def test_all_warehouses_calls_the_function_without_a_name(self) -> None:
        rows = [_credit_row(0, "WH_A"), _credit_row(1, "WH_B")]
        session = FakeSession(credit_rows=rows)
        result = run_tool(
            CREDITS_CASE,
            session,
            warehouse=None,
            all_warehouses=True,
            last_minutes=60,
        )
        function = session.statements[-1]
        assert "WAREHOUSE_NAME => ?" not in function.sql
        assert function.params == [NOW_MS - 60 * 60_000, NOW_MS, 51]
        assert [r["WAREHOUSE_NAME"] for r in result["rows"]] == ["WH_A", "WH_B"]

    def test_a_named_warehouse_is_bound_upper_case_and_filters(self) -> None:
        rows = [_credit_row(0, "WH_A"), _credit_row(1, "MY_WH")]
        session = FakeSession(credit_rows=rows)
        result = run_tool(CREDITS_CASE, session, warehouse="my_wh", last_minutes=60)
        assert session.statements[-1].params == [
            NOW_MS - 60 * 60_000,
            NOW_MS,
            "MY_WH",
            51,
        ]
        assert [r["WAREHOUSE_NAME"] for r in result["rows"]] == ["MY_WH"]

    def test_the_result_says_the_function_answers_only_with_the_privilege(self) -> None:
        notes = run_with_session(CREDITS_CASE, FakeSession())["notes"]
        assert len(notes) == 1
        assert "MONITOR USAGE" in notes[0]

    def test_credits_statement_text_for_one_warehouse(self) -> None:
        assert tools.build_credits_sql(by_warehouse=True) == (
            "SELECT WAREHOUSE_NAME, START_TIME, END_TIME, CREDITS_USED_COMPUTE, "
            "CREDITS_USED_CLOUD_SERVICES, CREDITS_USED "
            "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY("
            "DATE_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
            "DATE_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
            "WAREHOUSE_NAME => ?::STRING)) "
            "ORDER BY START_TIME DESC, WAREHOUSE_NAME LIMIT ?"
        )

    def test_credits_statement_text_for_all_warehouses(self) -> None:
        assert tools.build_credits_sql(by_warehouse=False) == (
            "SELECT WAREHOUSE_NAME, START_TIME, END_TIME, CREDITS_USED_COMPUTE, "
            "CREDITS_USED_CLOUD_SERVICES, CREDITS_USED "
            "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY("
            "DATE_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
            "DATE_RANGE_END => TO_TIMESTAMP_LTZ(?, 3))) "
            "ORDER BY START_TIME DESC, WAREHOUSE_NAME LIMIT ?"
        )


class TestEventTableRecords:
    """What only the event table tool promises."""

    LEVELS = {
        "SCHEMA": "SCH_DB.SCH_SCHEMA.SCH_EVENTS",
        "DATABASE": "DB_DB.DB_SCHEMA.DB_EVENTS",
        "ACCOUNT": "ACC_DB.ACC_SCHEMA.ACC_EVENTS",
    }

    def _read(self, tables: dict[str, str | Exception], **overrides: Any) -> Any:
        """Run the tool with `EVENT_TABLE` held by the given levels; return session and result."""
        session = FakeSession(event_tables=tables)
        arguments = {"schema": "MY_SCHEMA", "last_minutes": 60, **overrides}
        return session, run_tool(EVENT_CASE, session, **arguments)

    @pytest.mark.parametrize(
        ("held", "expected"),
        [
            (["SCHEMA", "DATABASE", "ACCOUNT"], "SCH_DB.SCH_SCHEMA.SCH_EVENTS"),
            (["DATABASE", "ACCOUNT"], "DB_DB.DB_SCHEMA.DB_EVENTS"),
            (["ACCOUNT"], "ACC_DB.ACC_SCHEMA.ACC_EVENTS"),
        ],
    )
    def test_the_nearest_level_that_holds_a_value_names_the_table(
        self, held: list[str], expected: str
    ) -> None:
        session, result = self._read({lv: self.LEVELS[lv] for lv in held})
        assert _data_statements(session)[0].params[0] == expected
        assert result["source"].startswith(expected)

    def test_the_levels_are_asked_in_order_and_stop_at_the_first_value(self) -> None:
        session, _ = self._read(dict(self.LEVELS))
        assert [
            s.sql for s in session.statements if s.sql.startswith("SHOW PARAMETERS")
        ] == ["SHOW PARAMETERS LIKE 'event_table' IN SCHEMA MY_DB.MY_SCHEMA"]

    def test_without_a_schema_only_database_then_account_are_asked(self) -> None:
        session, _ = self._read({"ACCOUNT": self.LEVELS["ACCOUNT"]}, schema=None)
        assert [
            s.sql for s in session.statements if s.sql.startswith("SHOW PARAMETERS")
        ] == [
            "SHOW PARAMETERS LIKE 'event_table' IN DATABASE MY_DB",
            "SHOW PARAMETERS LIKE 'event_table' IN ACCOUNT",
        ]

    def test_a_refused_level_is_skipped_and_named_not_an_error(self) -> None:
        refused = SqlRefused("Insufficient privileges on schema", 3001)
        session, result = self._read(
            {"SCHEMA": refused, "DATABASE": self.LEVELS["DATABASE"]}
        )
        assert _data_statements(session)[0].params[0] == self.LEVELS["DATABASE"]
        assert any("SCHEMA level" in note for note in result["notes"])

    def test_a_failure_that_is_not_a_refusal_is_raised_and_no_other_level_is_read(
        self,
    ) -> None:
        session = FakeSession(
            event_tables={
                "SCHEMA": ConnectionError("network dropped"),
                "DATABASE": self.LEVELS["DATABASE"],
            }
        )
        with pytest.raises(tools.PcSnowError, match="ConnectionError"):
            run_tool(EVENT_CASE, session, schema="MY_SCHEMA", last_minutes=60)
        shows = [
            s.sql for s in session.statements if s.sql.startswith("SHOW PARAMETERS")
        ]
        assert shows == ["SHOW PARAMETERS LIKE 'event_table' IN SCHEMA MY_DB.MY_SCHEMA"]
        assert _data_statements(session) == []

    def test_a_skipped_level_is_named_when_no_other_level_holds_a_value(self) -> None:
        refused = SqlRefused("Insufficient privileges on schema", 3001)
        session = FakeSession(event_tables={"SCHEMA": refused})
        with pytest.raises(tools.PcSnowError) as raised:
            run_tool(EVENT_CASE, session, schema="MY_SCHEMA", last_minutes=60)
        message = str(raised.value)
        assert "No EVENT_TABLE value found" in message
        assert "SCHEMA" in message
        assert "refused or not found" in message
        assert "Ask the user" in message

    def test_the_skip_note_says_error_2003_covers_refusal_and_absence(self) -> None:
        refused = SqlRefused("does not exist or not authorized", 2003)
        _session, result = self._read(
            {"SCHEMA": refused, "DATABASE": self.LEVELS["DATABASE"]}
        )
        assert any("2003 covers both" in note for note in result["notes"])

    def _shows(self, session: FakeSession) -> list[str]:
        """Every `SHOW` the session received."""
        return [s.sql for s in session.statements if s.sql.startswith("SHOW")]

    def test_a_misspelled_database_names_the_visible_ones_and_reads_nothing(
        self,
    ) -> None:
        session = FakeSession(databases={"MY_DB": ["MY_SCHEMA"], "OTHER_DB": []})
        with pytest.raises(tools.PcSnowError) as raised:
            run_tool(EVENT_CASE, session, database="MY_DBB", last_minutes=60)
        message = str(raised.value)
        assert "database 'MY_DBB' is not visible" in message
        assert "MY_DB, OTHER_DB" in message
        assert "Ask the user" in message
        assert not any(s.startswith("SHOW PARAMETERS") for s in self._shows(session))
        assert _data_statements(session) == []

    def test_a_misspelled_schema_names_the_visible_ones_and_reads_nothing(self) -> None:
        session = FakeSession(databases={"MY_DB": ["PUBLIC", "MY_SCHEMA"]})
        with pytest.raises(tools.PcSnowError) as raised:
            run_tool(EVENT_CASE, session, schema="MY_SCEMA", last_minutes=60)
        message = str(raised.value)
        assert "schema 'MY_SCEMA' is not visible" in message
        assert "in database MY_DB" in message
        assert "MY_SCHEMA, PUBLIC" in message
        assert not any(s.startswith("SHOW PARAMETERS") for s in self._shows(session))
        assert _data_statements(session) == []

    def test_visible_names_are_sorted_and_bounded_to_twenty(self) -> None:
        names = {f"DB_{n:02d}": [] for n in range(30, 0, -1)}
        session = FakeSession(databases=names)
        with pytest.raises(tools.PcSnowError) as raised:
            run_tool(EVENT_CASE, session, database="NOPE", last_minutes=60)
        message = str(raised.value)
        assert "DB_01, DB_02" in message
        assert "DB_20" in message
        assert "DB_21" not in message

    def test_existing_names_proceed_as_before_in_any_case(self) -> None:
        session = FakeSession(
            databases={"MY_DB": ["MY_SCHEMA"]},
            event_tables={"DATABASE": self.LEVELS["DATABASE"]},
        )
        result = run_tool(
            EVENT_CASE, session, database="my_db", schema="my_schema", last_minutes=60
        )
        assert result["source"].startswith(self.LEVELS["DATABASE"])
        assert self._shows(session)[:2] == [
            "SHOW TERSE DATABASES LIKE 'my_db'",
            "SHOW TERSE SCHEMAS LIKE 'my_schema' IN DATABASE my_db",
        ]

    def test_a_wildcard_match_is_not_a_match(self) -> None:
        session = FakeSession(databases={"MYXDB": ["MY_SCHEMA"]})
        with pytest.raises(tools.PcSnowError, match="not visible"):
            run_tool(EVENT_CASE, session, database="MY_DB", last_minutes=60)
        assert not any(s.startswith("SHOW PARAMETERS") for s in self._shows(session))
        session = FakeSession(databases={"MY_DB": ["MYXSCHEMA"]})
        with pytest.raises(
            tools.PcSnowError, match="schema 'MY_SCHEMA' is not visible"
        ):
            run_tool(EVENT_CASE, session, schema="MY_SCHEMA", last_minutes=60)

    @pytest.mark.parametrize(
        ("failing", "error", "expected"),
        [
            ("DATABASES", ConnectionError("network dropped"), "network dropped"),
            ("SCHEMAS", ConnectionError("network dropped"), "network dropped"),
            (
                "DATABASES",
                SqlRefused("Insufficient privileges to list databases", 3001),
                "Insufficient privileges to list databases",
            ),
        ],
        ids=["database-show-fails", "schema-show-fails", "database-show-refused"],
    )
    def test_a_failing_visibility_check_is_not_reported_as_a_missing_name(
        self, failing: str, error: Exception, expected: str
    ) -> None:
        session = FakeSession(
            databases={"MY_DB": ["MY_SCHEMA"]}, show_failures={failing: error}
        )
        with pytest.raises(tools.PcSnowError) as raised:
            run_tool(EVENT_CASE, session, schema="MY_SCHEMA", last_minutes=60)
        message = str(raised.value)
        assert expected in message
        assert "not visible" not in message
        assert "Visible " not in message
        assert not any(s.startswith("SHOW PARAMETERS") for s in self._shows(session))
        assert _data_statements(session) == []

    def test_visibility_statement_text(self) -> None:
        assert (
            tools.build_visible_names_sql("DATABASES", like="MY_DB")
            == "SHOW TERSE DATABASES LIKE 'MY_DB'"
        )
        assert tools.build_visible_names_sql("DATABASES") == "SHOW TERSE DATABASES"
        assert tools.build_visible_names_sql("SCHEMAS", "MY_DB", "S") == (
            "SHOW TERSE SCHEMAS LIKE 'S' IN DATABASE MY_DB"
        )
        assert tools.build_visible_names_sql("SCHEMAS", "MY_DB") == (
            "SHOW TERSE SCHEMAS IN DATABASE MY_DB"
        )
        for sql in (
            tools.build_visible_names_sql("DATABASES", like="MY_DB"),
            tools.build_visible_names_sql("SCHEMAS", "MY_DB"),
        ):
            tools.check_statement(sql)
        with pytest.raises(tools.PcSnowError):
            tools.build_visible_names_sql("DATABASES", like="x'; DROP")

    def test_every_level_refused_raises_the_last_refusal(self) -> None:
        refused = SqlRefused("Insufficient privileges on account", 3001)
        with pytest.raises(tools.PcSnowError, match="Insufficient privileges"):
            self._read({lv: refused for lv in self.LEVELS})

    def test_no_level_holding_a_value_asks_the_user_and_reads_nothing(self) -> None:
        session = FakeSession(event_tables={})
        with pytest.raises(tools.PcSnowError, match="Ask the user"):
            run_tool(EVENT_CASE, session, last_minutes=60)
        assert _data_statements(session) == []

    @pytest.mark.parametrize(
        "name", ['"my-db".s.t', "only_a_table", "db.table", "a.b.c.d", "a.b.c; DROP"]
    )
    def test_a_resolved_name_that_is_not_a_plain_identifier_is_refused(
        self, name: str
    ) -> None:
        session = FakeSession(event_tables={"ACCOUNT": name})
        with pytest.raises(tools.PcSnowError, match="not a plain"):
            run_tool(EVENT_CASE, session, last_minutes=60)
        assert _data_statements(session) == []

    def test_the_resolved_table_is_bound_never_joined_into_the_statement(self) -> None:
        session, _ = self._read({"ACCOUNT": self.LEVELS["ACCOUNT"]})
        function = _data_statements(session)[0]
        assert self.LEVELS["ACCOUNT"] not in function.sql
        assert "FROM IDENTIFIER(?)" in function.sql

    def test_a_query_id_keeps_only_that_querys_records(self) -> None:
        rows = [
            _event_row(1, query_id=TARGET_QUERY_ID, SEVERITY="ERROR"),
            _event_row(2),
            _event_row(3, query_id=TARGET_QUERY_ID, SEVERITY="WARN"),
        ]
        session = FakeSession(event_rows=rows)
        result = run_tool(
            EVENT_CASE, session, query_id=TARGET_QUERY_ID.upper(), last_minutes=60
        )
        function = _data_statements(session)[0]
        assert "RESOURCE_ATTRIBUTES['snow.query.id']::STRING = ?" in function.sql
        assert function.params == [
            "SNOWFLAKE.TELEMETRY.EVENTS",
            NOW_MS - 60 * 60_000,
            NOW_MS,
            TARGET_QUERY_ID,
            51,
        ]
        assert [r["SEVERITY"] for r in result["rows"]] == ["ERROR", "WARN"]

    def test_without_a_query_id_there_is_no_query_filter(self) -> None:
        session = FakeSession(event_rows=[_event_row(1), _event_row(2)])
        result = run_tool(EVENT_CASE, session, last_minutes=60)
        assert "snow.query.id']::STRING = ?" not in _data_statements(session)[0].sql
        assert result["row_count"] == 2

    def test_the_result_does_not_claim_an_empty_answer_for_a_missing_privilege(
        self,
    ) -> None:
        notes = run_with_session(EVENT_CASE, FakeSession())["notes"]
        assert "no row instead of an error" not in notes[0]
        assert "2003" in notes[0]
        assert "LOG_LEVEL and TRACE_LEVEL" in notes[0]

    def test_parameter_statement_text(self) -> None:
        assert tools.build_event_parameter_sql("SCHEMA", "MY_DB", "MY_SCHEMA") == (
            "SHOW PARAMETERS LIKE 'event_table' IN SCHEMA MY_DB.MY_SCHEMA"
        )
        assert tools.build_event_parameter_sql("DATABASE", "MY_DB", None) == (
            "SHOW PARAMETERS LIKE 'event_table' IN DATABASE MY_DB"
        )
        assert tools.build_event_parameter_sql("ACCOUNT", "MY_DB", None) == (
            "SHOW PARAMETERS LIKE 'event_table' IN ACCOUNT"
        )

    def test_records_statement_text(self) -> None:
        assert tools.build_event_records_sql(by_query_id=False, include_text=False) == (
            "SELECT TIMESTAMP, START_TIMESTAMP, RECORD_TYPE, "
            "RECORD['severity_text']::STRING AS SEVERITY, "
            "RESOURCE_ATTRIBUTES['snow.executable.name']::STRING AS EXECUTABLE_NAME, "
            "RESOURCE_ATTRIBUTES['snow.query.id']::STRING AS QUERY_ID "
            "FROM IDENTIFIER(?) "
            "WHERE TIMESTAMP >= TO_TIMESTAMP_NTZ(?, 3) "
            "AND TIMESTAMP < TO_TIMESTAMP_NTZ(?, 3) "
            "ORDER BY TIMESTAMP DESC LIMIT ?"
        )

    def test_records_statement_text_with_query_id_and_free_text(self) -> None:
        assert tools.build_event_records_sql(by_query_id=True, include_text=True) == (
            "SELECT TIMESTAMP, START_TIMESTAMP, RECORD_TYPE, "
            "RECORD['severity_text']::STRING AS SEVERITY, "
            "RESOURCE_ATTRIBUTES['snow.executable.name']::STRING AS EXECUTABLE_NAME, "
            "RESOURCE_ATTRIBUTES['snow.query.id']::STRING AS QUERY_ID, "
            "VALUE::STRING AS MESSAGE, RECORD_ATTRIBUTES::STRING AS ATTRIBUTES "
            "FROM IDENTIFIER(?) "
            "WHERE TIMESTAMP >= TO_TIMESTAMP_NTZ(?, 3) "
            "AND TIMESTAMP < TO_TIMESTAMP_NTZ(?, 3) "
            "AND RESOURCE_ATTRIBUTES['snow.query.id']::STRING = ? "
            "ORDER BY TIMESTAMP DESC LIMIT ?"
        )


class TestStatementGuard:
    """The guard every statement passes through."""

    @pytest.mark.parametrize(
        "sql",
        [
            "USE ROLE ACCOUNTADMIN",
            "CREATE TABLE t (a INT)",
            "DROP TABLE t",
            "SELECT 1; DROP TABLE t",
            "CALL my_proc()",
            "ALTER SESSION SET TIMEZONE = 'UTC'",
        ],
    )
    def test_anything_but_select_show_or_the_opening_statement_is_refused(
        self, sql: str
    ) -> None:
        with pytest.raises(tools.PcSnowError):
            tools.check_statement(sql)

    @pytest.mark.parametrize(
        "sql", ["SELECT 1", "  show tasks", "USE SECONDARY ROLES ALL"]
    )
    def test_select_show_and_the_opening_statement_pass(self, sql: str) -> None:
        tools.check_statement(sql)


# ── The server over stdio ────────────────────────────────────────────────────────────────────────

SERVER = PLUGIN / "mcp" / "pc_snow_server.py"


def _server_env(home: Path) -> dict[str, str]:
    """An environment with no Snowflake credentials of its own."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SNOWFLAKE_")}
    env["SNOWFLAKE_HOME"] = str(home)
    env.pop("PC_SNOW_TIMEZONE", None)
    return env


@pytest.fixture
def server(_clean_environment: Path) -> Iterator[StdioMcp]:
    """The real server in a subprocess, with no elicitation capability on the client."""
    pytest.importorskip("mcp")
    client = StdioMcp([sys.executable, str(SERVER)], _server_env(_clean_environment))
    client.initialize()
    yield client
    client.close()


class TestServerOverStdio:
    """The announced contract of the running server."""

    def test_tools_list_answers_without_credentials_and_opens_nothing(
        self, server: StdioMcp
    ) -> None:
        announced = server.list_tools()
        assert sorted(t["name"] for t in announced) == sorted(CASE_IDS)
        assert server.elicitation_requests == []

    def test_every_server_tool_has_a_case(self, server: StdioMcp) -> None:
        assert {t["name"] for t in server.list_tools()} == {c.name for c in TOOL_CASES}

    def test_each_tool_takes_exactly_its_listed_parameters(
        self, server: StdioMcp
    ) -> None:
        by_name = {t["name"]: t for t in server.list_tools()}
        for case in TOOL_CASES:
            assert sorted(by_name[case.name]["inputSchema"]["properties"]) == sorted(
                case.expected_params
            )

    def test_no_tool_takes_a_connection_a_profile_an_account_or_sql(
        self, server: StdioMcp
    ) -> None:
        banned = {
            "connection",
            "profile",
            "account",
            "sql",
            "query",
            "statement",
            "table",
            "event_table",
        }
        for tool in server.list_tools():
            assert banned.isdisjoint(tool["inputSchema"]["properties"]), tool["name"]

    def test_each_description_names_the_role_of_its_own_view_only(
        self, server: StdioMcp
    ) -> None:
        by_name = {t["name"]: t["description"] for t in server.list_tools()}
        for name, role in USAGE_ROLE.items():
            other = next(r for n, r in USAGE_ROLE.items() if n != name)
            assert role in by_name[name], name
            assert other not in by_name[name], name

    def test_the_credits_description_says_no_row_and_no_error(
        self, server: StdioMcp
    ) -> None:
        by_name = {t["name"]: t["description"] for t in server.list_tools()}
        text = " ".join(by_name["warehouse_credits"].split())
        assert "no row and no error" in text
        assert "otherwise the error names it" not in text

    def test_each_description_fits_in_ten_lines(self, server: StdioMcp) -> None:
        for tool in server.list_tools():
            assert len(tool["description"].splitlines()) <= 10, tool["name"]

    def test_stdout_carries_protocol_messages_only(self, server: StdioMcp) -> None:
        server.list_tools()
        server.call_tool("task_history", {})
        assert server.stdout_lines
        for line in server.stdout_lines:
            assert json.loads(line)["jsonrpc"] == "2.0"

    def test_a_call_without_scope_comes_back_as_an_instruction_to_ask(
        self, server: StdioMcp
    ) -> None:
        result = server.call_tool("query_history", {})
        assert result["isError"] is True
        assert "Ask the user" in result["content"][0]["text"]

    def test_the_script_header_declares_its_own_dependencies(self) -> None:
        header = SERVER.read_text(encoding="utf-8").split("# ///")[1]
        assert "snowflake-snowpark-python" in header
        assert "mcp" in header

    def test_mcp_json_launches_the_server_through_uv(self) -> None:
        declared = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
        launch = declared["pc-snow"]
        assert launch["command"] == "uv"
        assert launch["args"] == [
            "run",
            "--script",
            "${CLAUDE_PLUGIN_ROOT}/mcp/pc_snow_server.py",
        ]


@pytest.mark.skipif(
    importlib.util.find_spec("snowflake") is not None,
    reason="the opening step would reach for a real connection when Snowpark is importable",
)
class TestProfileQuestionOverStdio:
    """The profile question as a real MCP client sees it."""

    def _two_profiles(self, home: Path) -> None:
        _write_profiles(home, "alpha", "beta")

    def test_the_question_is_a_single_choice_among_the_configured_profiles(
        self, _clean_environment: Path
    ) -> None:
        self._two_profiles(_clean_environment)
        client = StdioMcp(
            [sys.executable, str(SERVER)],
            _server_env(_clean_environment),
            elicit=lambda params: {"action": "accept", "content": {"profile": "alpha"}},
        )
        try:
            client.initialize()
            assert client.elicitation_requests == []
            client.call_tool("task_history", dict(TASK_CASE.scope, last_minutes=5))
            properties = client.elicitation_requests[0]["requestedSchema"]["properties"]
            assert properties["profile"]["enum"] == ["alpha", "beta"]
        finally:
            client.close()

    def test_a_declined_question_names_the_variable(
        self, _clean_environment: Path
    ) -> None:
        self._two_profiles(_clean_environment)
        client = StdioMcp(
            [sys.executable, str(SERVER)],
            _server_env(_clean_environment),
            elicit=lambda params: {"action": "decline"},
        )
        try:
            client.initialize()
            result = client.call_tool(
                "task_history", dict(TASK_CASE.scope, last_minutes=5)
            )
            assert result["isError"] is True
            assert "SNOWFLAKE_DEFAULT_CONNECTION_NAME" in result["content"][0]["text"]
        finally:
            client.close()

    def test_a_client_that_cannot_be_asked_gets_the_same_error(
        self, _clean_environment: Path
    ) -> None:
        self._two_profiles(_clean_environment)
        client = StdioMcp(
            [sys.executable, str(SERVER)], _server_env(_clean_environment)
        )
        try:
            client.initialize()
            result = client.call_tool(
                "task_history", dict(TASK_CASE.scope, last_minutes=5)
            )
            assert result["isError"] is True
            assert "SNOWFLAKE_DEFAULT_CONNECTION_NAME" in result["content"][0]["text"]
            assert client.elicitation_requests == []
        finally:
            client.close()


# ── The knowledge the tools ship with ────────────────────────────────────────────────────────────

REFERENCE = PLUGIN / "skills" / "snowflake" / "references" / "system-views.md"


def _section(text: str, heading: str) -> str:
    """The body of the `## heading` section, up to the next `## `."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match is not None, heading
    return match.group(1)


class TestShippedKnowledge:
    """The reference, the skill index and the README name what the server announces."""

    @pytest.mark.parametrize("case", TOOL_CASES, ids=CASE_IDS)
    def test_reference_has_a_section_per_tool(self, case: ToolCase) -> None:
        text = REFERENCE.read_text(encoding="utf-8")
        assert f"\n## {case.name}\n" in text

    @pytest.mark.parametrize("case", TOOL_CASES, ids=CASE_IDS)
    def test_interview_names_every_scope_and_range_parameter(
        self, case: ToolCase
    ) -> None:
        interview = _section(
            REFERENCE.read_text(encoding="utf-8"),
            "Interview protocol — ask before calling",
        )
        for parameter in case.scope_params + ["start", "end", "last_minutes"]:
            assert f"`{parameter}`" in interview, parameter
        assert f"`{case.name}`" in interview

    def test_skill_index_lists_the_reference(self) -> None:
        skill = (PLUGIN / "skills" / "snowflake" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "`references/system-views.md`" in skill

    @pytest.mark.parametrize("case", TOOL_CASES, ids=CASE_IDS)
    def test_readme_names_uv_and_each_tool(self, case: ToolCase) -> None:
        readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
        assert "`uv`" in readme
        assert f"`{case.name}`" in readme

    def test_no_machine_or_organisation_value_is_shipped_under_the_plugin(self) -> None:
        shipped = [
            p
            for p in PLUGIN.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        ]
        assert shipped
        pattern = re.compile(r"my-connection|PINKY_|DEPLOYER|DEVPINKY")
        offenders = [
            str(p.relative_to(PLUGIN))
            for p in shipped
            if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
        ]
        assert offenders == []
