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
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
        fail_with: Exception | None = None,
        session_timezone: str = "Pacific/Auckland",
        fail_on_open: Exception | None = None,
    ) -> None:
        """Set the data the functions hold and an optional failure for the function statement."""
        self.task_rows = task_rows or []
        self.query_rows = query_rows or []
        self.fail_with = fail_with
        self.fail_on_open = fail_on_open
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
        if self.fail_with is not None:
            raise self.fail_with
        if ".TASK_HISTORY(" in text:
            return _Collected(self._task_function(text, bound))
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

    def request(self, **overrides: Any) -> Any:
        """Validate a call: this case's scope unless overridden, every other argument at rest."""
        arguments: dict[str, Any] = {name: None for name in self.expected_params}
        arguments.update({"limit": 50, "include_text": False})
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
)
TOOL_CASES = [TASK_CASE, QUERY_CASE]
CASE_IDS = [case.name for case in TOOL_CASES]


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


def _rows_for(case: ToolCase, count: int, **extra: Any) -> list[dict[str, Any]]:
    """`count` history rows of the kind this tool reads."""
    make = _task_row if case is TASK_CASE else _query_row
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
        assert [s for s in session.issued() if "FROM TABLE(" in s] == []

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
        arguments: dict[str, Any] = {name: None for name in case.expected_params}
        arguments.update({"limit": 50, "include_text": False, "last_minutes": 60})
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
        with pytest.raises(tools.PcSnowError, match="7-day"):
            case.request(start="2026-10-01T00:00", end="2026-10-11T00:00")
        with pytest.raises(tools.PcSnowError, match="7-day"):
            case.request(last_minutes=7 * 24 * 60 + 1)

    def test_a_range_that_started_over_seven_days_ago_is_never_sent(
        self, case: ToolCase
    ) -> None:
        session = FakeSession()
        old_start = datetime.fromtimestamp(NOW_MS / 1000, UTC) - timedelta(days=9)
        with pytest.raises(tools.PcSnowError, match="7 days"):
            run_tool(
                case,
                session,
                start=old_start.isoformat(),
                end=(old_start + timedelta(hours=1)).isoformat(),
            )
        assert [s for s in session.issued() if "FROM TABLE(" in s] == []

    def test_the_age_limit_of_an_absolute_start_is_the_largest_last_minutes(
        self, case: ToolCase
    ) -> None:
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
        assert [s for s in session.issued() if "FROM TABLE(" in s] == []
        newest = stale + timedelta(minutes=1)
        accepted = FakeSession()
        run_tool(
            case,
            accepted,
            start=newest.isoformat(),
            end=(newest + timedelta(hours=1)).isoformat(),
        )
        assert len([s for s in accepted.issued() if "FROM TABLE(" in s]) == 1

    def test_a_range_that_has_not_begun_is_never_sent(self, case: ToolCase) -> None:
        session = FakeSession()
        with pytest.raises(tools.PcSnowError, match="already begun"):
            run_tool(case, session, start="2099-01-01", end="2099-01-02")
        assert [s for s in session.issued() if "FROM TABLE(" in s] == []

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
        with pytest.raises(tools.PcSnowError, match="7-day"):
            case.request(last_minutes=largest + 1)
        lookup = QUERY_CASE.request(query_id=TARGET_QUERY_ID, warehouse=None)
        assert lookup.plan.minutes == largest

    def test_every_statement_is_a_fixed_select_or_show(self, case: ToolCase) -> None:
        session = FakeSession()
        run_with_session(case, session, include_text=True)
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
        function = next(s for s in session.statements if "FROM TABLE(" in s.sql)
        bound = [v for key, v in case.scope.items() if key != "database"]
        for value in bound:
            assert value not in function.sql
            assert value in function.params

    def test_last_minutes_is_read_from_snowflakes_clock(self, case: ToolCase) -> None:
        session = FakeSession()
        before = int(time.time() * 1000)
        run_with_session(case, session, last_minutes=30)
        resolve, function = session.statements[1], session.statements[2]
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
        stamp = "SCHEDULED_TIME" if case is TASK_CASE else "START_TIME"
        assert row[stamp] == "2026-10-12T15:00:00.000+02:00"

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
        long_text = "x" * 500
        text = {"QUERY_TEXT": long_text, "ERROR_MESSAGE": "boom"}
        make = _task_row if case is TASK_CASE else _query_row
        session = FakeSession(**{case.rows_attr: [make(0, **text)]})
        default = run_with_session(case, session)
        assert "QUERY_TEXT" not in default["rows"][0]
        assert "ERROR_MESSAGE" not in default["rows"][0]
        assert "QUERY_TEXT" not in session.statements[-1].sql
        asked = run_with_session(case, session, include_text=True)
        assert asked["rows"][0]["QUERY_TEXT"] == "x" * 200
        assert asked["rows"][0]["ERROR_MESSAGE"] == "boom"
        assert asked["truncated_fields"] == [
            {"row": 0, "field": "QUERY_TEXT", "original_length": 500}
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
        banned = {"connection", "profile", "account", "sql", "query", "statement"}
        for tool in server.list_tools():
            assert banned.isdisjoint(tool["inputSchema"]["properties"]), tool["name"]

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
