"""pc-snow tool logic — scope, range and date contract, guarded statements, bounded output.

`pc_snow_server.py` is the MCP entry point and carries no logic; everything a tool does lives here
and receives its Snowflake session as a parameter, so this module imports nothing from `snowflake`
and the default test suite never needs Snowpark.

Contract shared by every tool:

- Scope is a required parameter. A call without it is refused with an instruction to ask the user;
  nothing falls back to the session's current database, schema or warehouse.
- A time range is required (`start` + `end`, or `last_minutes`), except where a `query_id` stands in
  for it. `last_minutes` is resolved by Snowflake's clock, never the machine's.
- A date without an offset is read in the user's timezone (`PC_SNOW_TIMEZONE`, else the system
  zone). The Snowflake session `TIMEZONE` is never read; ranges reach Snowflake as epoch values.
- `task_history` and `query_history` read `ACCOUNT_USAGE` when the range starts beyond the
  `INFORMATION_SCHEMA` window. Access is probed once per view and per process, kept in memory
  only, and a refusal names the `SNOWFLAKE` database role that covers that view.
- Every emitted statement is a fixed `SELECT` or `SHOW` with bound values, apart from the single
  `USE SECONDARY ROLES ALL` issued when the session opens.
- Output is bounded: at most `MAX_LIMIT` rows, text fields cut at `MAX_TEXT_CHARS`, free text
  (query text, error messages, log bodies) only when the caller sets `include_text`.

The connection is the connector's own default (`default_connection_name` in `config.toml`, or
`SNOWFLAKE_DEFAULT_CONNECTION_NAME`); the server adds a profile name only after the user chose one.

Errors: on mcp 2.2.0 only a `ToolError` carries its message to the model; any other exception
reaches it as `Error executing tool <name>` with no cause. The server turns `PcSnowError` into a
`ToolError`, and any new tool that fails on purpose must raise one of the two.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HISTORY_DAYS = 7
HISTORY_MINUTES = HISTORY_DAYS * 24 * 60
# Marge d'une minute : l'horloge de Snowflake est lue avant l'instruction qui porte la plage, et
# la fonction refuse un début plus ancien que 7 jours à son propre instant. Cette durée est à la
# fois le plus grand `last_minutes` accepté et la fenêtre d'une recherche par `query_id`.
MAX_RANGE_MINUTES = HISTORY_MINUTES - 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TEXT_CHARS = 200
FUNCTION_ROW_CEILING = 10_000
# Rétention des vues ACCOUNT_USAGE, la borne du repli au-delà de la fenêtre des fonctions.
ACCOUNT_USAGE_DAYS = 365
ACCOUNT_USAGE_MINUTES = ACCOUNT_USAGE_DAYS * 24 * 60
_SECONDARY_ROLES_SQL = "USE SECONDARY ROLES ALL"
_NOW_SQL = "SELECT DATE_PART('EPOCH_MILLISECOND', CURRENT_TIMESTAMP()) AS NOW_MS"
_ROLES_SQL = (
    "SELECT CURRENT_ROLE() AS PRIMARY_ROLE, "
    "CURRENT_SECONDARY_ROLES() AS SECONDARY_ROLES"
)
_STATEMENT_START = re.compile(r"\s*(SELECT|SHOW)\b", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")
_QUERY_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_TABLE_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$]{0,254}(\.[A-Za-z_][A-Za-z0-9_$]{0,254}){2}"
)
_PRIVILEGE_TEXT = re.compile(
    r"insufficient privileges|not authorized|permission denied|access denied",
    re.IGNORECASE,
)
_PRIVILEGE_ERROR_CODES = frozenset({2003, 3001})
# Le rôle de base de données SNOWFLAKE qui couvre chaque vue : l'un ne couvre pas l'autre.
_USAGE_ROLES = {
    "TASK_HISTORY": "USAGE_VIEWER",
    "QUERY_HISTORY": "GOVERNANCE_VIEWER",
    "DYNAMIC_TABLE_REFRESH_HISTORY": "USAGE_VIEWER",
}

TASK_COLUMNS = [
    "NAME",
    "DATABASE_NAME",
    "SCHEMA_NAME",
    "STATE",
    "ERROR_CODE",
    "SCHEDULED_TIME",
    "QUERY_START_TIME",
    "COMPLETED_TIME",
    "SCHEDULED_FROM",
    "QUERY_ID",
    "GRAPH_RUN_GROUP_ID",
]
TASK_TEXT_COLUMNS = ["ERROR_MESSAGE", "RETURN_VALUE", "QUERY_TEXT"]
QUERY_COLUMNS = [
    "QUERY_ID",
    "USER_NAME",
    "ROLE_NAME",
    "WAREHOUSE_NAME",
    "WAREHOUSE_SIZE",
    "QUERY_TYPE",
    "EXECUTION_STATUS",
    "ERROR_CODE",
    "START_TIME",
    "END_TIME",
    "TOTAL_ELAPSED_TIME",
    "QUEUED_OVERLOAD_TIME",
    "BYTES_SCANNED",
    "ROWS_PRODUCED",
    "QUERY_TAG",
]
QUERY_TEXT_COLUMNS = ["ERROR_MESSAGE", "QUERY_TEXT"]
# Les seules colonnes sur lesquelles `query_history` agrège : whitelist, jamais une valeur libre
# (un GROUP BY ne se lie pas, la colonne est validée puis interpolée).
QUERY_GROUPABLE = (
    "QUERY_TYPE",
    "EXECUTION_STATUS",
    "USER_NAME",
    "ROLE_NAME",
    "WAREHOUSE_NAME",
    "ERROR_CODE",
)
QUERY_GROUP_COLUMNS = [
    "GROUP_VALUE",
    "QUERIES",
    "RUNNING",
    "TOTAL_ELAPSED_TIME_MS",
    "QUEUED_OVERLOAD_TIME_MS",
    "BYTES_SCANNED",
    "ROWS_PRODUCED",
]
CREDIT_COLUMNS = [
    "WAREHOUSE_NAME",
    "START_TIME",
    "END_TIME",
    "CREDITS_USED_COMPUTE",
    "CREDITS_USED_CLOUD_SERVICES",
    "CREDITS_USED",
]
EVENT_COLUMNS = [
    "TIMESTAMP",
    "START_TIMESTAMP",
    "RECORD_TYPE",
    "SEVERITY",
    "EXECUTABLE_NAME",
    "QUERY_ID",
]
EVENT_TEXT_COLUMNS = ["MESSAGE", "ATTRIBUTES"]
OPERATOR_COLUMNS = [
    "QUERY_ID",
    "STEP_ID",
    "OPERATOR_ID",
    "PARENT_OPERATORS",
    "OPERATOR_TYPE",
    "OPERATOR_STATISTICS",
    "EXECUTION_TIME_BREAKDOWN",
    "OPERATOR_ATTRIBUTES",
]
OPERATOR_VARIANT_COLUMNS = [
    "OPERATOR_STATISTICS",
    "EXECUTION_TIME_BREAKDOWN",
    "OPERATOR_ATTRIBUTES",
]
DT_COLUMNS = [
    "NAME",
    "SCHEMA_NAME",
    "DATABASE_NAME",
    "STATE",
    "QUERY_ID",
    "DATA_TIMESTAMP",
    "REFRESH_START_TIME",
    "REFRESH_END_TIME",
    "COMPLETION_TARGET",
]
DT_TEXT_COLUMNS = ["STATE_MESSAGE", "STATISTICS"]
DT_VARIANT_COLUMNS = ["STATISTICS"]
WH_CONFIG_COLUMNS = [
    "NAME",
    "STATE",
    "TYPE",
    "SIZE",
    "MIN_CLUSTER_COUNT",
    "MAX_CLUSTER_COUNT",
    "STARTED_CLUSTERS",
    "RUNNING",
    "QUEUED",
    "AUTO_SUSPEND",
    "AUTO_RESUME",
    "SCALING_POLICY",
    "RESOURCE_CONSTRAINT",
    "ENABLE_QUERY_ACCELERATION",
    "QUERY_ACCELERATION_MAX_SCALE_FACTOR",
    "QUERY_THROUGHPUT_MULTIPLIER",
    "MAX_QUERY_PERFORMANCE_LEVEL",
    "RESOURCE_MONITOR",
    "OWNER",
    "COMMENT",
]
_QUERY_ID_ATTRIBUTE = "RESOURCE_ATTRIBUTES['snow.query.id']::STRING"
_EVENT_EXPRESSIONS = {
    "TIMESTAMP": "TIMESTAMP",
    "START_TIMESTAMP": "START_TIMESTAMP",
    "RECORD_TYPE": "RECORD_TYPE",
    "SEVERITY": "RECORD['severity_text']::STRING AS SEVERITY",
    "EXECUTABLE_NAME": "RESOURCE_ATTRIBUTES['snow.executable.name']::STRING AS EXECUTABLE_NAME",
    "QUERY_ID": f"{_QUERY_ID_ATTRIBUTE} AS QUERY_ID",
    "MESSAGE": "VALUE::STRING AS MESSAGE",
    "ATTRIBUTES": "RECORD_ATTRIBUTES::STRING AS ATTRIBUTES",
}

_FRESHNESS = (
    "near real time (INFORMATION_SCHEMA function, not the delayed ACCOUNT_USAGE view); "
    f"covers the last {HISTORY_DAYS} days only"
)
_TASK_PRIVILEGE = (
    "OWNERSHIP, MONITOR or OPERATE on the task (or the global MONITOR EXECUTION), "
    "plus USAGE on its database and schema"
)
_QUERY_PRIVILEGE = (
    "MONITOR or OPERATE on the warehouse or on the task, or MONITOR EXECUTION on the account, "
    "to see other users' queries; the connected user's own queries need none"
)
_CREDITS_PRIVILEGE = (
    "MONITOR USAGE (a global privilege) or the ACCOUNTADMIN role; "
    "without either, the function returns nothing"
)
_CREDITS_NOTE = (
    "Rows are hourly, one per warehouse per hour, ordered newest first. The function answers "
    "only for ACCOUNTADMIN or a role granted MONITOR USAGE: an empty result may mean the "
    "privilege is missing rather than that no credits were used."
)
_EVENT_FRESHNESS = (
    "event table read directly (not an INFORMATION_SCHEMA function): records are written "
    f"asynchronously and may lag the event; this tool covers the last {HISTORY_DAYS} days only"
)
_EVENT_PRIVILEGE = (
    "SELECT on the resolved event table, plus USAGE on its database and schema"
)
_EVENT_NOTE = (
    "An empty result does not prove nothing was recorded: a missing privilege on the resolved "
    "table came back as an error (2003, measured), and an event table holds only what "
    "LOG_LEVEL and TRACE_LEVEL let through."
)
_EVENT_LEVELS = ("SCHEMA", "DATABASE", "ACCOUNT")
_OPERATOR_FRESHNESS = (
    "GET_QUERY_OPERATOR_STATS table function: only completed queries of the past 14 days; "
    "a query still running or older answers no row"
)
_OPERATOR_PRIVILEGE = (
    "the query must be visible to the active role, exactly as in QUERY_HISTORY: "
    "the connected user's own queries need nothing, other users' need MONITOR or OPERATE "
    "on their warehouse or task, or MONITOR EXECUTION"
)
_OPERATOR_NOTE = (
    "One row per query operator, ordered by OPERATOR_ID. The VARIANT fields "
    "(OPERATOR_STATISTICS, EXECUTION_TIME_BREAKDOWN, OPERATOR_ATTRIBUTES) are returned "
    "parsed, not cut. An empty result means the query is still running, is older than 14 "
    "days, or is not visible to the active role."
)
_WH_CONFIG_FRESHNESS = (
    "live configuration read (SHOW WAREHOUSES): no history, the current values"
)
_WH_CONFIG_PRIVILEGE = (
    "any privilege on the warehouse (USAGE, MONITOR, OPERATE, OWNERSHIP): a warehouse the "
    "active role cannot see is absent from the list, so an empty result does not prove the "
    "warehouse does not exist"
)
_WH_CONFIG_NOTE = (
    "RUNNING and QUEUED are the live query counts on the warehouse at read time. An empty "
    "result means the name does not exist or the active role cannot see it; ask the user."
)
_DT_PRIVILEGE = (
    "MONITOR on the dynamic table (or OWNERSHIP), plus USAGE on its database and schema; "
    "with less, the function answers no row and no error"
)
_DT_NOTE = (
    "One row per refresh, newest first. The function answers only what the active role can "
    "monitor: an empty result may mean the privilege is missing rather than that no refresh "
    "happened. error_only=true keeps FAILED, UPSTREAM_FAILED and CANCELLED refreshes."
)
_USAGE_FRESHNESS = (
    "delayed: SNOWFLAKE.ACCOUNT_USAGE view, which lags the event, so the latest "
    f"executions may be missing; covers the last {ACCOUNT_USAGE_DAYS} days"
)


class PcSnowError(Exception):
    """A refusal or failure whose message is returned to the model verbatim."""


ProfileAsker = Callable[[list[str]], Awaitable[str | None]]


# ── Timezone and dates ───────────────────────────────────────────────────────────────────────────


class UserZone:
    """The user's timezone: `PC_SNOW_TIMEZONE` (IANA name) when set, else the machine's zone."""

    def __init__(self, key: str | None) -> None:
        """Resolve `key` as an IANA zone; `None` or empty selects the system zone."""
        self._zone: ZoneInfo | None = None
        if key:
            try:
                self._zone = ZoneInfo(key)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise PcSnowError(
                    f"PC_SNOW_TIMEZONE={key!r} is not an IANA timezone name "
                    "(e.g. Europe/Paris). Unset it to use the system timezone."
                ) from exc
            self.name = key
        else:
            self.name = datetime.now().astimezone().tzname() or "local"

    def _read(self, naive: datetime) -> datetime:
        """Attach this zone to a naive datetime, honouring its `fold`."""
        if self._zone is not None:
            return naive.replace(tzinfo=self._zone)
        return naive.astimezone()

    def localize(self, naive: datetime, label: str) -> datetime:
        """Read a naive wall-clock datetime in this zone; a daylight-saving gap or fold is refused."""
        first = self._read(naive.replace(fold=0))
        second = self._read(naive.replace(fold=1))
        if first.utcoffset() == second.utcoffset():
            return first
        exists = self.convert(first.astimezone(UTC)).replace(tzinfo=None) == naive
        what = (
            "is ambiguous (the clock goes back)"
            if exists
            else "does not exist (the clock goes forward)"
        )
        raise PcSnowError(
            f"{label}={naive.isoformat()!r} {what} in {self.name}. Ask the user for an "
            f"explicit offset: {first.isoformat()} or {second.isoformat()}."
        )

    def convert(self, aware: datetime) -> datetime:
        """Express an aware datetime in this zone."""
        if self._zone is not None:
            return aware.astimezone(self._zone)
        return aware.astimezone()


def user_zone(environ: Mapping[str, str] | None = None) -> UserZone:
    """Build the user's zone from `PC_SNOW_TIMEZONE`; the session `TIMEZONE` is never consulted."""
    env = os.environ if environ is None else environ
    return UserZone(env.get("PC_SNOW_TIMEZONE"))


def parse_instant(label: str, value: str, zone: UserZone) -> datetime:
    """Parse an ISO-8601 date or datetime; an absent offset means the user's zone."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise PcSnowError(
            f"{label}={value!r} is not an ISO-8601 date or datetime "
            "(e.g. 2026-10-12T15:00 or 2026-10-12T15:00+02:00)."
        ) from exc
    if parsed.tzinfo is None:
        parsed = zone.localize(parsed, label)
    return parsed


def _epoch_ms(moment: datetime) -> int:
    """Whole milliseconds since the epoch."""
    return round(moment.timestamp() * 1000)


def _iso(epoch_ms: int, zone: UserZone) -> str:
    """Render an epoch instant in the user's zone, offset included."""
    moment = datetime.fromtimestamp(epoch_ms / 1000, UTC)
    return zone.convert(moment).isoformat(timespec="milliseconds")


# ── Scope and range validation ───────────────────────────────────────────────────────────────────


def _ask_scope(tool: str, missing: Sequence[str]) -> PcSnowError:
    """The refusal for a call that lacks its scope."""
    return PcSnowError(
        f"{tool}: missing required scope: {', '.join(missing)}. Ask the user for "
        "each of these values; do not infer them from the session's current "
        "database, schema or warehouse, and do not search for them."
    )


def _identifier(label: str, value: str) -> str:
    """Accept an unquoted Snowflake identifier; anything else is refused, never escaped."""
    candidate = value.strip()
    if not _IDENTIFIER.fullmatch(candidate):
        raise PcSnowError(
            f"{label}={value!r} is not a plain identifier: letters, digits, `_` and `$`, "
            "not starting with a digit. Quoted identifiers are not supported."
        )
    return candidate


@dataclass(frozen=True)
class RangePlan:
    """A validated time range, before Snowflake's clock is read.

    `kind` is `absolute` (`start_ms`/`end_ms` set), `relative` (`minutes` set) or `lookback`
    (a `query_id` without a range: the whole history window).
    """

    kind: str
    start_ms: int | None = None
    end_ms: int | None = None
    minutes: int | None = None


def plan_range(
    start: str | None,
    end: str | None,
    last_minutes: int | None,
    zone: UserZone,
    *,
    accepts_query_id: bool,
    query_id_given: bool = False,
    account_usage: bool = False,
) -> RangePlan:
    """Validate the range arguments; no statement is emitted for a refused range.

    `account_usage` lets a range reach back past the `INFORMATION_SCHEMA` window, up to the
    retention of the `ACCOUNT_USAGE` views, for a tool that falls back to them.
    """
    if account_usage:
        max_minutes = ACCOUNT_USAGE_MINUTES
        bound = f"{ACCOUNT_USAGE_DAYS}-day retention of ACCOUNT_USAGE views"
        max_span = timedelta(days=ACCOUNT_USAGE_DAYS)
    else:
        max_minutes = MAX_RANGE_MINUTES
        bound = f"{HISTORY_DAYS}-day limit of INFORMATION_SCHEMA functions"
        max_span = timedelta(days=HISTORY_DAYS)
    if last_minutes is not None and (start or end):
        raise PcSnowError(
            "last_minutes cannot be combined with start or end: give either "
            "start and end, or last_minutes."
        )
    if last_minutes is not None:
        if last_minutes < 1:
            raise PcSnowError("last_minutes must be at least 1.")
        if last_minutes > max_minutes:
            raise PcSnowError(
                f"last_minutes={last_minutes} exceeds the {bound} "
                f"(at most {max_minutes} minutes)."
            )
        return RangePlan("relative", minutes=last_minutes)
    if start or end:
        if not (start and end):
            raise PcSnowError(
                "An absolute range needs both start and end. Ask the user for the missing bound."
            )
        first = parse_instant("start", start, zone)
        last = parse_instant("end", end, zone)
        if last <= first:
            raise PcSnowError("end must be after start.")
        if last - first > max_span:
            raise PcSnowError(
                f"The range {start} .. {end} is longer than the {bound}. "
                "Ask the user for a shorter range."
            )
        return RangePlan("absolute", start_ms=_epoch_ms(first), end_ms=_epoch_ms(last))
    if query_id_given:
        return RangePlan("lookback", minutes=MAX_RANGE_MINUTES)
    asked = "a time range (start and end, or last_minutes)"
    if accepts_query_id:
        asked += " and a query_id if they have one"
    raise PcSnowError(f"No time range given. Ask the user for {asked}.")


def _check_limit(limit: int) -> int:
    """Row limit within `1..MAX_LIMIT`."""
    if limit < 1 or limit > MAX_LIMIT:
        raise PcSnowError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}.")
    return limit


@dataclass(frozen=True)
class TaskRequest:
    """A validated `task_history` call."""

    database: str
    schema: str
    task: str
    plan: RangePlan
    limit: int
    include_text: bool
    zone: UserZone


@dataclass(frozen=True)
class QueryRequest:
    """A validated `query_history` call; `group_by` is `None` for row-level output."""

    query_id: str | None
    warehouse: str | None
    group_by: str | None
    plan: RangePlan
    limit: int
    include_text: bool
    zone: UserZone


@dataclass(frozen=True)
class CreditsRequest:
    """A validated `warehouse_credits` call; `warehouse` is `None` for all warehouses."""

    warehouse: str | None
    plan: RangePlan
    limit: int
    zone: UserZone


@dataclass(frozen=True)
class EventRequest:
    """A validated `event_table_records` call; `schema` is `None` when the user gave none."""

    database: str
    schema: str | None
    query_id: str | None
    plan: RangePlan
    limit: int
    include_text: bool
    zone: UserZone


@dataclass(frozen=True)
class OperatorStatsRequest:
    """A validated `query_operator_stats` call: one completed query, no range."""

    query_id: str
    limit: int
    zone: UserZone


@dataclass(frozen=True)
class WarehouseConfigRequest:
    """A validated `warehouse_config` call; `warehouse` is `None` for all warehouses."""

    warehouse: str | None
    limit: int
    zone: UserZone


@dataclass(frozen=True)
class DynamicTableRequest:
    """A validated `dynamic_table_refresh_history` call.

    `database`/`schema`/`name` scope one dynamic table; `name_prefix` scopes several by name.
    Exactly one of the two is set.
    """

    database: str | None
    schema: str | None
    name: str | None
    name_prefix: str | None
    error_only: bool
    plan: RangePlan
    limit: int
    include_text: bool
    zone: UserZone


def plan_task_history(
    database: str | None,
    schema: str | None,
    task: str | None,
    start: str | None,
    end: str | None,
    last_minutes: int | None,
    limit: int,
    include_text: bool,
    environ: Mapping[str, str] | None = None,
) -> TaskRequest:
    """Validate a `task_history` call: scope first, then range, then limit."""
    if not (database and schema and task):
        missing = [
            name
            for name, value in (
                ("database", database),
                ("schema", schema),
                ("task", task),
            )
            if not value
        ]
        raise _ask_scope("task_history", missing)
    zone = user_zone(environ)
    plan = plan_range(
        start, end, last_minutes, zone, accepts_query_id=False, account_usage=True
    )
    return TaskRequest(
        database=_identifier("database", database),
        schema=_identifier("schema", schema),
        task=_identifier("task", task),
        plan=plan,
        limit=_check_limit(limit),
        include_text=include_text,
        zone=zone,
    )


def plan_query_history(
    query_id: str | None,
    warehouse: str | None,
    start: str | None,
    end: str | None,
    last_minutes: int | None,
    limit: int,
    include_text: bool,
    group_by: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> QueryRequest:
    """Validate a `query_history` call: a `query_id` or a warehouse, then range, then limit."""
    if not query_id and not warehouse:
        raise _ask_scope("query_history", ["query_id or warehouse"])
    if query_id and not _QUERY_ID.fullmatch(query_id.strip()):
        raise PcSnowError(f"query_id={query_id!r} is not a query id (a UUID).")
    if group_by is not None:
        if group_by.upper() not in QUERY_GROUPABLE:
            raise PcSnowError(
                f"group_by={group_by!r} is not a column query_history aggregates: "
                f"{', '.join(QUERY_GROUPABLE)}."
            )
        if query_id:
            raise PcSnowError(
                "query_history: group_by aggregates a set of queries; it cannot be combined "
                "with a single query_id. Drop one of the two."
            )
        if include_text:
            raise PcSnowError(
                "query_history: group_by returns aggregates, not rows; include_text has "
                "nothing to show. Drop one of the two."
            )
    zone = user_zone(environ)
    plan = plan_range(
        start,
        end,
        last_minutes,
        zone,
        accepts_query_id=True,
        query_id_given=bool(query_id),
        account_usage=True,
    )
    return QueryRequest(
        query_id=query_id.strip().lower() if query_id else None,
        warehouse=_identifier("warehouse", warehouse).upper() if warehouse else None,
        group_by=group_by.upper() if group_by else None,
        plan=plan,
        limit=_check_limit(limit),
        include_text=include_text,
        zone=zone,
    )


def plan_operator_stats(
    query_id: str | None,
    limit: int,
    environ: Mapping[str, str] | None = None,
) -> OperatorStatsRequest:
    """Validate a `query_operator_stats` call: one query id, then limit; there is no range."""
    if not query_id:
        raise _ask_scope("query_operator_stats", ["query_id"])
    if not _QUERY_ID.fullmatch(query_id.strip()):
        raise PcSnowError(f"query_id={query_id!r} is not a query id (a UUID).")
    return OperatorStatsRequest(
        query_id=query_id.strip().lower(),
        limit=_check_limit(limit),
        zone=user_zone(environ),
    )


def plan_warehouse_config(
    warehouse: str | None,
    all_warehouses: bool | None,
    limit: int,
    environ: Mapping[str, str] | None = None,
) -> WarehouseConfigRequest:
    """Validate a `warehouse_config` call: one warehouse or `all_warehouses`, no range."""
    if warehouse and all_warehouses:
        raise PcSnowError(
            "warehouse_config: give a warehouse or all_warehouses=true, not both. "
            "Ask the user which one they mean."
        )
    if not warehouse and not all_warehouses:
        raise PcSnowError(
            "warehouse_config: missing required scope: warehouse or all_warehouses=true. "
            "Ask the user which warehouse, or whether they want all of them; omitting the "
            "name never means all, and do not infer it from the session or search for it."
        )
    return WarehouseConfigRequest(
        warehouse=_identifier("warehouse", warehouse).upper() if warehouse else None,
        limit=_check_limit(limit),
        zone=user_zone(environ),
    )


def plan_dynamic_table_refresh(
    database: str | None,
    schema: str | None,
    name: str | None,
    name_prefix: str | None,
    error_only: bool,
    start: str | None,
    end: str | None,
    last_minutes: int | None,
    limit: int,
    include_text: bool,
    environ: Mapping[str, str] | None = None,
) -> DynamicTableRequest:
    """Validate a `dynamic_table_refresh_history` call: one table or a prefix, then range, limit."""
    by_name = any([database, schema, name])
    if by_name and name_prefix:
        raise PcSnowError(
            "dynamic_table_refresh_history: give database+schema+name or name_prefix, not both. "
            "Ask the user which one they mean."
        )
    if by_name and not (database and schema and name):
        missing = [
            label
            for label, value in (
                ("database", database),
                ("schema", schema),
                ("name", name),
            )
            if not value
        ]
        raise PcSnowError(
            f"dynamic_table_refresh_history: a dynamic table needs database, schema and name; "
            f"missing: {', '.join(missing)}. Ask the user for the missing values, or give "
            "name_prefix instead."
        )
    if not by_name and not name_prefix:
        raise _ask_scope(
            "dynamic_table_refresh_history", ["database+schema+name or name_prefix"]
        )
    if name_prefix is not None:
        _identifier("name_prefix", name_prefix)
    zone = user_zone(environ)
    plan = plan_range(
        start, end, last_minutes, zone, accepts_query_id=False, account_usage=True
    )
    return DynamicTableRequest(
        database=_identifier("database", database) if database else None,
        schema=_identifier("schema", schema) if schema else None,
        name=_identifier("name", name) if name else None,
        name_prefix=name_prefix,
        error_only=error_only,
        plan=plan,
        limit=_check_limit(limit),
        include_text=include_text,
        zone=zone,
    )


def plan_credits(
    warehouse: str | None,
    all_warehouses: bool | None,
    start: str | None,
    end: str | None,
    last_minutes: int | None,
    limit: int,
    environ: Mapping[str, str] | None = None,
) -> CreditsRequest:
    """Validate a `warehouse_credits` call: one warehouse or `all_warehouses`, then range, limit."""
    if warehouse and all_warehouses:
        raise PcSnowError(
            "warehouse_credits: give a warehouse or all_warehouses=true, not both. "
            "Ask the user which one they mean."
        )
    if not warehouse and not all_warehouses:
        raise PcSnowError(
            "warehouse_credits: missing required scope: warehouse or all_warehouses=true. "
            "Ask the user which warehouse, or whether they want all of them; omitting the "
            "name never means all, and do not infer it from the session or search for it."
        )
    zone = user_zone(environ)
    plan = plan_range(start, end, last_minutes, zone, accepts_query_id=False)
    return CreditsRequest(
        warehouse=_identifier("warehouse", warehouse).upper() if warehouse else None,
        plan=plan,
        limit=_check_limit(limit),
        zone=zone,
    )


def plan_event_records(
    database: str | None,
    schema: str | None,
    query_id: str | None,
    start: str | None,
    end: str | None,
    last_minutes: int | None,
    limit: int,
    include_text: bool,
    environ: Mapping[str, str] | None = None,
) -> EventRequest:
    """Validate an `event_table_records` call: database, then range, then limit."""
    if not database:
        raise _ask_scope("event_table_records", ["database"])
    if query_id and not _QUERY_ID.fullmatch(query_id.strip()):
        raise PcSnowError(f"query_id={query_id!r} is not a query id (a UUID).")
    zone = user_zone(environ)
    plan = plan_range(start, end, last_minutes, zone, accepts_query_id=False)
    return EventRequest(
        database=_identifier("database", database),
        schema=_identifier("schema", schema) if schema else None,
        query_id=query_id.strip().lower() if query_id else None,
        plan=plan,
        limit=_check_limit(limit),
        include_text=include_text,
        zone=zone,
    )


# ── Statements ───────────────────────────────────────────────────────────────────────────────────


def build_task_history_sql(database: str, include_text: bool) -> str:
    """The `TASK_HISTORY` statement for one database; `database` is a validated identifier."""
    columns = TASK_COLUMNS + (TASK_TEXT_COLUMNS if include_text else [])
    # Seuls l'identifiant de base (validé) et la liste de colonnes sont interpolés ; toute valeur
    # de l'appelant est liée.
    return (
        f"SELECT {', '.join(columns)} "  # nosec B608
        f"FROM TABLE({database}.INFORMATION_SCHEMA.TASK_HISTORY("
        "SCHEDULED_TIME_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
        "SCHEDULED_TIME_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
        f"RESULT_LIMIT => {FUNCTION_ROW_CEILING}, "
        "DATABASE_NAME => ?::STRING, "
        "SCHEMA_NAME => ?::STRING, "
        "TASK_NAME => ?::STRING)) "
        "ORDER BY SCHEDULED_TIME DESC LIMIT ?"
    )


def build_query_history_sql(
    by_warehouse: bool, by_query_id: bool, include_text: bool
) -> str:
    """The `QUERY_HISTORY` statement, in its plain or `_BY_WAREHOUSE` form."""
    columns = QUERY_COLUMNS + (QUERY_TEXT_COLUMNS if include_text else [])
    function = "QUERY_HISTORY_BY_WAREHOUSE" if by_warehouse else "QUERY_HISTORY"
    warehouse_arg = "WAREHOUSE_NAME => ?::STRING, " if by_warehouse else ""
    where = "WHERE QUERY_ID = ? " if by_query_id else ""
    # Seuls le nom de fonction (deux constantes) et la liste de colonnes sont interpolés ; toute
    # valeur de l'appelant est liée.
    return (
        f"SELECT {', '.join(columns)} "  # nosec B608
        f"FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.{function}("
        f"{warehouse_arg}"
        "END_TIME_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
        "END_TIME_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
        f"RESULT_LIMIT => {FUNCTION_ROW_CEILING})) "
        f"{where}"
        "ORDER BY START_TIME DESC LIMIT ?"
    )


def _usage_source(source: str) -> str:
    """A validated `db.schema.view` name an ACCOUNT_USAGE statement is built against."""
    if not _TABLE_NAME.fullmatch(source):
        raise PcSnowError(f"{source!r} is not a plain database.schema.view name.")
    return source


def build_account_usage_task_history_sql(source: str, include_text: bool) -> str:
    """The `TASK_HISTORY` statement over the resolved view; scope and range are bound."""
    columns = TASK_COLUMNS + (TASK_TEXT_COLUMNS if include_text else [])
    # Seuls la vue résolue (validée) et la liste de colonnes (constantes) sont interpolés ;
    # toute valeur de l'appelant est liée.
    return (
        f"SELECT {', '.join(columns)} "  # nosec B608
        f"FROM {_usage_source(source)} "
        "WHERE SCHEDULED_TIME >= TO_TIMESTAMP_LTZ(?, 3) "
        "AND SCHEDULED_TIME < TO_TIMESTAMP_LTZ(?, 3) "
        "AND DATABASE_NAME = ? AND SCHEMA_NAME = ? AND NAME = ? "
        "ORDER BY SCHEDULED_TIME DESC LIMIT ?"
    )


def build_account_usage_query_history_sql(
    source: str, by_warehouse: bool, by_query_id: bool, include_text: bool
) -> str:
    """The `QUERY_HISTORY` statement over the resolved view, with the optional filters."""
    columns = QUERY_COLUMNS + (QUERY_TEXT_COLUMNS if include_text else [])
    warehouse = "AND WAREHOUSE_NAME = ? " if by_warehouse else ""
    query_id = "AND QUERY_ID = ? " if by_query_id else ""
    # Seuls la vue résolue (validée), la liste de colonnes (constantes) et la présence des filtres
    # sont interpolés ; toute valeur de l'appelant est liée.
    return (
        f"SELECT {', '.join(columns)} "  # nosec B608
        f"FROM {_usage_source(source)} "
        "WHERE END_TIME >= TO_TIMESTAMP_LTZ(?, 3) "
        "AND END_TIME < TO_TIMESTAMP_LTZ(?, 3) "
        f"{warehouse}{query_id}"
        "ORDER BY START_TIME DESC LIMIT ?"
    )


# Les agrégats d'un group_by : les requêtes RUNNING sont exclues des durées (leur END_TIME est
# epoch 0, ce qui donnerait des sommes aberrantes) et comptées à part.
_GROUP_SELECT = (
    "{column} AS GROUP_VALUE, "
    "COUNT(*) AS QUERIES, "
    "SUM(IFF(EXECUTION_STATUS = 'RUNNING', 1, 0)) AS RUNNING, "
    "SUM(CASE WHEN EXECUTION_STATUS <> 'RUNNING' THEN TOTAL_ELAPSED_TIME END) "
    "AS TOTAL_ELAPSED_TIME_MS, "
    "SUM(QUEUED_OVERLOAD_TIME) AS QUEUED_OVERLOAD_TIME_MS, "
    "SUM(BYTES_SCANNED) AS BYTES_SCANNED, "
    "SUM(ROWS_PRODUCED) AS ROWS_PRODUCED"
)


def _group_column(group_by: str) -> str:
    """A `group_by` value already validated by the plan; revalidated before interpolation."""
    if group_by not in QUERY_GROUPABLE:
        raise PcSnowError(f"{group_by!r} is not a column query_history aggregates.")
    return group_by


def build_query_history_grouped_sql(by_warehouse: bool, group_by: str) -> str:
    """The grouped `QUERY_HISTORY` statement; the column is whitelisted, values bound."""
    column = _group_column(group_by)
    function = "QUERY_HISTORY_BY_WAREHOUSE" if by_warehouse else "QUERY_HISTORY"
    warehouse_arg = "WAREHOUSE_NAME => ?::STRING, " if by_warehouse else ""
    # Seuls la colonne d'agrégation (whitelist), le nom de fonction (deux constantes) et la liste
    # de colonnes (constantes) sont interpolés ; toute valeur de l'appelant est liée.
    return (
        f"SELECT {_GROUP_SELECT.format(column=column)} "  # nosec B608
        f"FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.{function}("
        f"{warehouse_arg}"
        "END_TIME_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
        "END_TIME_RANGE_END => TO_TIMESTAMP_LTZ(?, 3), "
        f"RESULT_LIMIT => {FUNCTION_ROW_CEILING})) "
        f"GROUP BY {column} ORDER BY QUERIES DESC LIMIT ?"
    )


def build_account_usage_query_grouped_sql(
    source: str, by_warehouse: bool, group_by: str
) -> str:
    """The grouped `QUERY_HISTORY` statement over the resolved view."""
    column = _group_column(group_by)
    warehouse = "AND WAREHOUSE_NAME = ? " if by_warehouse else ""
    # Seuls la colonne d'agrégation (whitelist), la vue résolue (validée) et la présence du
    # filtre sont interpolés ; toute valeur de l'appelant est liée.
    return (
        f"SELECT {_GROUP_SELECT.format(column=column)} "  # nosec B608
        f"FROM {_usage_source(source)} "
        "WHERE END_TIME >= TO_TIMESTAMP_LTZ(?, 3) "
        "AND END_TIME < TO_TIMESTAMP_LTZ(?, 3) "
        f"{warehouse}"
        f"GROUP BY {column} ORDER BY QUERIES DESC LIMIT ?"
    )


def build_warehouse_config_sql(warehouse: str | None) -> str:
    """The `SHOW WAREHOUSES` statement for one warehouse or all; the name is a validated identifier."""
    if warehouse is None:
        return "SHOW WAREHOUSES"
    return f"SHOW WAREHOUSES LIKE '{_identifier('warehouse', warehouse)}'"


def build_credits_totals_sql(by_warehouse: bool) -> str:
    """The `WAREHOUSE_METERING_HISTORY` sums over the whole range; no row limit."""
    warehouse_arg = ", WAREHOUSE_NAME => ?::STRING" if by_warehouse else ""
    # Seuls la liste de colonnes (constante) et la présence de l'argument d'entrepôt sont
    # interpolées ; toute valeur de l'appelant est liée.
    return (
        "SELECT SUM(CREDITS_USED_COMPUTE) AS CREDITS_USED_COMPUTE, "  # nosec B608
        "SUM(CREDITS_USED_CLOUD_SERVICES) AS CREDITS_USED_CLOUD_SERVICES, "
        "SUM(CREDITS_USED) AS CREDITS_USED "
        "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY("
        "DATE_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
        f"DATE_RANGE_END => TO_TIMESTAMP_LTZ(?, 3){warehouse_arg}))"
    )


def build_credits_sql(by_warehouse: bool) -> str:
    """The `WAREHOUSE_METERING_HISTORY` statement, with or without a warehouse name argument."""
    warehouse_arg = ", WAREHOUSE_NAME => ?::STRING" if by_warehouse else ""
    # Seuls la liste de colonnes (constante) et la présence de l'argument d'entrepôt sont
    # interpolées ; toute valeur de l'appelant est liée.
    return (
        f"SELECT {', '.join(CREDIT_COLUMNS)} "  # nosec B608
        "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY("
        "DATE_RANGE_START => TO_TIMESTAMP_LTZ(?, 3), "
        f"DATE_RANGE_END => TO_TIMESTAMP_LTZ(?, 3){warehouse_arg})) "
        "ORDER BY START_TIME DESC, WAREHOUSE_NAME LIMIT ?"
    )


def build_event_parameter_sql(level: str, database: str, schema: str | None) -> str:
    """The `SHOW PARAMETERS` statement for `EVENT_TABLE` at one level; names are validated."""
    if level == "SCHEMA" and schema:
        target = f"SCHEMA {database}.{schema}"
    elif level == "DATABASE":
        target = f"DATABASE {database}"
    elif level == "ACCOUNT":
        target = "ACCOUNT"
    else:
        raise PcSnowError(f"No {level} level can be asked for EVENT_TABLE here.")
    return f"SHOW PARAMETERS LIKE 'event_table' IN {target}"


def build_event_records_sql(by_query_id: bool, include_text: bool) -> str:
    """The event table `SELECT`; the table name is bound through `IDENTIFIER(?)`, never joined."""
    columns = EVENT_COLUMNS + (EVENT_TEXT_COLUMNS if include_text else [])
    where = f"AND {_QUERY_ID_ATTRIBUTE} = ? " if by_query_id else ""
    # Seuls la liste de colonnes (constantes) et la présence du filtre sont interpolées ; le nom de
    # table et toute valeur de l'appelant sont liés.
    return (
        f"SELECT {', '.join(_EVENT_EXPRESSIONS[c] for c in columns)} "  # nosec B608
        "FROM IDENTIFIER(?) "
        "WHERE TIMESTAMP >= TO_TIMESTAMP_NTZ(?, 3) "
        "AND TIMESTAMP < TO_TIMESTAMP_NTZ(?, 3) "
        f"{where}"
        "ORDER BY TIMESTAMP DESC LIMIT ?"
    )


def build_operator_stats_sql() -> str:
    """The `GET_QUERY_OPERATOR_STATS` statement; the query id is bound, never joined."""
    # Seule la liste de colonnes (constantes) est interpolée ; l'identifiant de requête est lié.
    return (
        f"SELECT {', '.join(OPERATOR_COLUMNS)} "  # nosec B608
        "FROM TABLE(GET_QUERY_OPERATOR_STATS(?::STRING)) "
        "ORDER BY OPERATOR_ID LIMIT ?"
    )


def build_dynamic_table_refresh_sql(
    qualified_name: str | None,
    name_prefix: str | None,
    error_only: bool,
    include_text: bool,
) -> str:
    """The `DYNAMIC_TABLE_REFRESH_HISTORY` statement; the table and prefix are bound."""
    columns = DT_COLUMNS + (DT_TEXT_COLUMNS if include_text else [])
    name_arg = "NAME => ?::STRING, " if qualified_name else ""
    prefix_arg = "NAME_PREFIX => ?::STRING, " if name_prefix else ""
    error_arg = "ERROR_ONLY => TRUE, " if error_only else ""
    # Seuls la liste de colonnes (constantes) et la présence des arguments sont interpolés ;
    # toute valeur de l'appelant est liée.
    return (
        f"SELECT {', '.join(columns)} "  # nosec B608
        "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.DYNAMIC_TABLE_REFRESH_HISTORY("
        "DATA_TIMESTAMP_START => TO_TIMESTAMP_LTZ(?, 3), "
        "DATA_TIMESTAMP_END => TO_TIMESTAMP_LTZ(?, 3), "
        f"{name_arg}{prefix_arg}{error_arg}"
        f"RESULT_LIMIT => {FUNCTION_ROW_CEILING})) "
        "ORDER BY DATA_TIMESTAMP DESC LIMIT ?"
    )


def build_account_usage_dynamic_table_sql(
    source: str,
    by_name: bool,
    by_prefix: bool,
    error_only: bool,
    include_text: bool,
) -> str:
    """The refresh-history statement over the resolved view, with the optional filters."""
    columns = DT_COLUMNS + (DT_TEXT_COLUMNS if include_text else [])
    name = "AND DATABASE_NAME = ? AND SCHEMA_NAME = ? AND NAME = ? " if by_name else ""
    prefix = "AND NAME LIKE ? || '%' " if by_prefix else ""
    errors = (
        "AND STATE IN ('FAILED', 'UPSTREAM_FAILED', 'CANCELLED') " if error_only else ""
    )
    # Seuls la vue résolue (validée), la liste de colonnes (constantes) et la présence des filtres
    # sont interpolés ; toute valeur de l'appelant est liée.
    return (
        f"SELECT {', '.join(columns)} "  # nosec B608
        f"FROM {_usage_source(source)} "
        "WHERE DATA_TIMESTAMP >= TO_TIMESTAMP_LTZ(?, 3) "
        "AND DATA_TIMESTAMP < TO_TIMESTAMP_LTZ(?, 3) "
        f"{name}{prefix}{errors}"
        "ORDER BY DATA_TIMESTAMP DESC LIMIT ?"
    )


def build_visible_names_sql(
    kind: str, database: str | None = None, like: str | None = None
) -> str:
    """A `SHOW TERSE DATABASES` or `SHOW TERSE SCHEMAS IN DATABASE` statement; names are validated."""
    if kind not in ("DATABASES", "SCHEMAS"):
        raise PcSnowError(f"No {kind} listing can be asked for here.")
    like_clause = f" LIKE '{_identifier('name', like)}'" if like else ""
    if kind == "SCHEMAS":
        if not database:
            raise PcSnowError("A schema listing needs the database it is read in.")
        return f"SHOW TERSE SCHEMAS{like_clause} IN DATABASE {_identifier('database', database)}"
    return f"SHOW TERSE DATABASES{like_clause}"


def check_statement(sql: str) -> None:
    """Refuse anything that is not a single `SELECT` or `SHOW`, or the opening role statement."""
    text = sql.strip()
    if text == _SECONDARY_ROLES_SQL:
        return
    if not _STATEMENT_START.match(text) or ";" in text:
        raise PcSnowError(
            f"Refused a statement that is not a single SELECT or SHOW: {text[:60]!r}"
        )


Runner = Callable[[str, Sequence[Any]], list[Any]]


def guarded_runner(session: Any) -> Runner:
    """Wrap a session so every statement passes `check_statement` before it is sent."""

    def run(sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Run one checked statement and collect its rows."""
        check_statement(sql)
        if params:
            return list(session.sql(sql, params=list(params)).collect())
        return list(session.sql(sql).collect())

    return run


# ── Rows ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldTruncation:
    """One text field cut at `MAX_TEXT_CHARS`."""

    row: int
    field: str
    original_length: int


def _row_dict(row: Any) -> dict[str, Any]:
    """A result row as a plain mapping with upper-case column names."""
    if hasattr(row, "as_dict"):
        raw = row.as_dict()
    else:
        raw = dict(row)
    return {str(key).upper(): value for key, value in raw.items()}


def _render_value(value: Any, zone: UserZone) -> Any:
    """A JSON-safe value; timestamps are rendered ISO-8601 in the user's zone with their offset."""
    if value is None or isinstance(value, bool | int | float | str | dict | list):
        # Les VARIANT de Snowflake (OPERATOR_STATISTICS, STATISTICS…) arrivent déjà en
        # dict/list natifs : on les garde tels quels, le JSON de sortie les porte.
        return value
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return zone.convert(aware).isoformat(timespec="milliseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return str(value)


def _shape_rows(
    rows: Sequence[Any],
    columns: Sequence[str],
    zone: UserZone,
    variant_columns: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[FieldTruncation]]:
    """Keep only the allowed columns, render values, cut text and record each cut.

    A `variant_columns` string holding JSON is parsed into its dict/list: the connector
    returns VARIANT as JSON text, and these fields are the point of the tools that carry
    them, so they are never cut at `MAX_TEXT_CHARS` the way free text is.
    """
    wanted = set(columns)
    variants = {c.upper() for c in variant_columns}
    shaped: list[dict[str, Any]] = []
    cuts: list[FieldTruncation] = []
    for index, row in enumerate(rows):
        out: dict[str, Any] = {}
        for column, value in _row_dict(row).items():
            if column not in wanted:
                continue
            rendered = _render_value(value, zone)
            if column in variants and isinstance(rendered, str):
                try:
                    rendered = json.loads(rendered)
                except ValueError:
                    # Pas du JSON malgré la déclaration : on garde le texte tel quel.
                    pass
            if isinstance(rendered, str) and len(rendered) > MAX_TEXT_CHARS:
                cuts.append(FieldTruncation(index, column, len(rendered)))
                rendered = rendered[:MAX_TEXT_CHARS]
            out[column] = rendered
        shaped.append(out)
    return shaped, cuts


def _result(
    *,
    source: str,
    plan_start_ms: int,
    plan_end_ms: int,
    rows: Sequence[Any],
    columns: Sequence[str],
    limit: int,
    zone: UserZone,
    notes: list[str],
    freshness: str = _FRESHNESS,
    variant_columns: Sequence[str] = (),
) -> dict[str, Any]:
    """The common result envelope: source, freshness, resolved range, bounded rows."""
    kept = list(rows)[:limit]
    shaped, cuts = _shape_rows(kept, columns, zone, variant_columns)
    return {
        "source": source,
        "freshness": freshness,
        "timezone": zone.name,
        "range": {
            "start": _iso(plan_start_ms, zone),
            "end": _iso(plan_end_ms, zone),
            "timezone": zone.name,
        },
        "limit": limit,
        "row_count": len(shaped),
        "truncated": len(rows) > limit,
        "truncated_fields": [asdict(cut) for cut in cuts],
        "notes": notes,
        "rows": shaped,
    }


# ── Execution ────────────────────────────────────────────────────────────────────────────────────


def _error_text(exc: BaseException) -> str:
    """Snowflake's own message for an exception, bounded."""
    text = getattr(exc, "message", None) or getattr(exc, "msg", None) or str(exc)
    return str(text).strip()[:1000]


_SESSION_EXPIRED_CODES = frozenset({390111, 390112, 390114})
_SESSION_EXPIRED_TEXT = re.compile(
    r"session (no longer exists|has expired|expired)|authentication token has expired",
    re.IGNORECASE,
)


def _is_session_expired(exc: BaseException) -> bool:
    """Whether the session died (idle timeout, token expired) and a fresh one may succeed."""
    code = getattr(exc, "sql_error_code", None) or getattr(exc, "errno", None)
    return code in _SESSION_EXPIRED_CODES or bool(
        _SESSION_EXPIRED_TEXT.search(_error_text(exc))
    )


def _has_privilege_code(exc: BaseException) -> bool:
    """Whether the error carries one of Snowflake's privilege error codes."""
    code = getattr(exc, "sql_error_code", None) or getattr(exc, "errno", None)
    return code in _PRIVILEGE_ERROR_CODES


def _is_privilege_error(exc: BaseException) -> bool:
    """Whether Snowflake refused the call for lack of a privilege (by code, else by text)."""
    return _has_privilege_code(exc) or bool(_PRIVILEGE_TEXT.search(_error_text(exc)))


def _active_roles(run: Runner) -> str:
    """The primary and secondary roles of the session, for a privilege report."""
    try:
        row = _row_dict(run(_ROLES_SQL, ())[0])
    except Exception as exc:
        # Le rapport ne doit pas masquer le refus d'origine.
        return f"unavailable ({type(exc).__name__})"
    return (
        f"CURRENT_ROLE()={row.get('PRIMARY_ROLE')}; "
        f"CURRENT_SECONDARY_ROLES()={row.get('SECONDARY_ROLES')}"
    )


def _snowflake_now_ms(run: Runner) -> int:
    """Snowflake's clock, in epoch milliseconds."""
    return int(_row_dict(run(_NOW_SQL, ())[0])["NOW_MS"])


def _resolve_window(
    run: Runner, plan: RangePlan, *, account_usage: bool = False
) -> tuple[int, int, bool]:
    """Start and end in epoch ms, read against Snowflake's clock, and whether the window is stale.

    A start older than the `INFORMATION_SCHEMA` window is stale: refused unless `account_usage`
    is set, in which case the caller reads the `ACCOUNT_USAGE` view and the flag is `True`.
    """
    now_ms = _snowflake_now_ms(run)
    if plan.start_ms is not None and plan.end_ms is not None:
        if plan.start_ms > now_ms:
            raise PcSnowError(
                "The range starts in the future by Snowflake's clock, so it holds no history. "
                "Ask the user for a range that has already begun."
            )
        start_ms, end_ms = plan.start_ms, plan.end_ms
    else:
        minutes = plan.minutes if plan.minutes is not None else MAX_RANGE_MINUTES
        start_ms, end_ms = now_ms - minutes * 60_000, now_ms
    if start_ms >= now_ms - MAX_RANGE_MINUTES * 60_000:
        return start_ms, end_ms, False
    if not account_usage:
        raise PcSnowError(
            f"The range starts more than {HISTORY_DAYS} days ago, beyond what "
            "INFORMATION_SCHEMA functions keep. Ask the user for a more recent range."
        )
    if start_ms < now_ms - ACCOUNT_USAGE_MINUTES * 60_000:
        raise PcSnowError(
            f"The range starts more than {ACCOUNT_USAGE_DAYS} days ago, beyond what "
            "ACCOUNT_USAGE views keep. Ask the user for a more recent range."
        )
    return start_ms, end_ms, True


def _resolve_span(run: Runner, plan: RangePlan) -> tuple[int, int]:
    """Start and end in epoch ms for a tool that has no `ACCOUNT_USAGE` fallback."""
    start_ms, end_ms, _stale = _resolve_window(run, plan)
    return start_ms, end_ms


def _usage_note(view: str, source: str) -> str:
    """The note that says why the result comes from `ACCOUNT_USAGE` and where it was read."""
    native = f"SNOWFLAKE.ACCOUNT_USAGE.{view}"
    if source.upper() == native.upper():
        return (
            f"The range reaches back more than {HISTORY_DAYS} days, which INFORMATION_SCHEMA "
            "functions do not keep: it was read from ACCOUNT_USAGE. That view is account-wide "
            f"and delayed, and needs the SNOWFLAKE.{_USAGE_ROLES[view]} database role."
        )
    return (
        f"The range reaches back more than {HISTORY_DAYS} days, which INFORMATION_SCHEMA "
        "functions do not keep: it was read from "
        f"{source}, a visible view discovered in place of {native} (refused; it needs the "
        f"SNOWFLAKE.{_USAGE_ROLES[view]} database role). It is account-wide-shaped and "
        "delayed, and may be filtered to the objects of one team or environment."
    )


class UsageAccess:
    """The `ACCOUNT_USAGE`-shaped view each tool reads when the range passes the function window.

    Per view and per process, one answer, held in memory and never written anywhere: a grant made
    later is seen when the server restarts. Resolution order for a view such as `QUERY_HISTORY`:

    1. `SNOWFLAKE.ACCOUNT_USAGE.<view>` itself, measured by a trial `SELECT` — kept as granted
       only on success, as denied only on a privilege error code; any other failure is raised
       and the next call tries again.
    2. On a denial, discovery: `SHOW VIEWS LIKE '<view>' IN ACCOUNT` lists what the role can
       see (`SHOW` only lists visible objects, so a refused `ACCOUNT_USAGE` never appears).
       A view whose name matches `<view>` exactly, case-insensitively, becomes the source; an
       optional `PC_SNOW_USAGE_LOCATION=db.schema` pins the discovery to one location when
       several views share the name (e.g. one per environment).
    3. No candidate: the denial is raised, naming the SNOWFLAKE database role that covers the
       view and the discovery outcome.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        """Start with no answer for any view."""
        self._lock = threading.Lock()
        self._environ = os.environ if environ is None else environ
        # Per view: the resolved source, or the refusal to raise again. A failure that is not
        # a privilege answer is never kept — the next call probes again.
        self._sources: dict[str, str | PcSnowError] = {}

    def resolve(self, run: Runner, view: str) -> str:
        """The fully qualified view `view` is read from; raise the grant instruction if none."""
        if view not in _USAGE_ROLES:
            raise PcSnowError(f"{view} is not an ACCOUNT_USAGE view this server reads.")
        with self._lock:
            if view not in self._sources:
                self._sources[view] = self._resolve(run, view)
            kept = self._sources[view]
        if isinstance(kept, PcSnowError):
            raise kept
        return kept

    def _resolve(self, run: Runner, view: str) -> str | PcSnowError:
        """Probe the native view, then discover a same-named visible one."""
        native = f"SNOWFLAKE.ACCOUNT_USAGE.{view}"
        probe = f"SELECT 1 AS PROBE FROM {native} LIMIT 1"  # nosec B608
        try:
            run(probe, ())
        except PcSnowError:
            raise
        except Exception as exc:
            if not _is_privilege_error(exc):
                raise
            # Une erreur de texte seul (proxy) n'est pas conservée : l'appel suivant re-sonde.
            cacheable = _has_privilege_code(exc)
            denial = self._discover(run, view, _error_text(exc))
            if isinstance(denial, PcSnowError) and not cacheable:
                raise denial
            return denial
        return native

    def _discover(self, run: Runner, view: str, refusal: str) -> str | PcSnowError:
        """The one visible view named exactly `view`, pinned or unique; else the denial."""
        rows = run(f"SHOW VIEWS LIKE '{view}' IN ACCOUNT", ())
        candidates = []
        for row in rows:
            found = _row_dict(row)
            name = str(found.get("NAME", ""))
            if name.upper() != view.upper():
                continue
            candidates.append(
                f"{found.get('DATABASE_NAME', '')}.{found.get('SCHEMA_NAME', '')}.{name}"
            )
        location = self._environ.get("PC_SNOW_USAGE_LOCATION", "").strip()
        if location:
            pinned = [
                c for c in candidates if c.upper().startswith(location.upper() + ".")
            ]
            if len(pinned) == 1:
                return pinned[0]
            raise PcSnowError(
                f"PC_SNOW_USAGE_LOCATION={location!r} pins no visible view named {view}: "
                f"candidates are {', '.join(candidates) if candidates else 'none'}. "
                "Fix or unset it."
            )
        if len(candidates) == 1:
            return candidates[0]
        return _usage_denied(run, view, refusal, candidates)

    def forget(self, view: str) -> None:
        """Drop the kept answer for `view`; the next `resolve` probes again."""
        with self._lock:
            self._sources.pop(view, None)


def _usage_denied(
    run: Runner, view: str, refusal: str, candidates: list[str] | None = None
) -> PcSnowError:
    """The refusal that names the database role covering `view`, with the roles of the session."""
    role = f"SNOWFLAKE.{_USAGE_ROLES[view]}"
    if candidates:
        discovery = (
            f"Several visible views are named {view}: {', '.join(sorted(candidates))}. "
            "Ask the user which one to read, then pin it with "
            "PC_SNOW_USAGE_LOCATION=db.schema."
        )
    else:
        discovery = f"No visible view named {view} was found either."
    return PcSnowError(
        f"Snowflake refused ACCOUNT_USAGE.{view}: {refusal}\n"
        f"The range reaches back more than {HISTORY_DAYS} days, which only ACCOUNT_USAGE holds.\n"
        f"Missing privilege: the {role} database role. ACCOUNTADMIN grants it "
        f"with GRANT DATABASE ROLE {role} TO ROLE <role>; the access to each view is probed "
        "once per process, so restart or reconnect the server afterwards.\n"
        f"{discovery}\n"
        f"Active roles: {_active_roles(run)}"
    )


def _read_account_usage(
    run: Runner,
    usage: UsageAccess,
    view: str,
    build_sql: Callable[[str], str],
    params: Sequence[Any],
) -> tuple[str, list[Any]]:
    """Resolve the view `view` is read from, then read it; returns the source and the rows.

    A refusal on the resolved view names the role that covers the native one; a discovered
    view that fails is forgotten so the next call resolves again.
    """
    source = usage.resolve(run, view)
    try:
        return source, run(build_sql(source), params)
    except PcSnowError:
        raise
    except Exception as exc:
        if _is_privilege_error(exc):
            if not source.upper().startswith("SNOWFLAKE.ACCOUNT_USAGE."):
                usage.forget(view)
            raise _usage_denied(run, view, _error_text(exc)) from exc
        raise


def _guarded_execution(
    run: Runner, required_privilege: str, action: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Run `action`; a Snowflake failure becomes an error that names the cause and the roles."""
    try:
        return action()
    except PcSnowError:
        raise
    except Exception as exc:
        message = _error_text(exc)
        if _is_privilege_error(exc):
            raise PcSnowError(
                f"Snowflake refused the call: {message}\n"
                f"Missing privilege: {required_privilege}\n"
                f"Active roles: {_active_roles(run)}"
            ) from exc
        raise PcSnowError(
            f"Snowflake rejected the statement: {type(exc).__name__}: {message}"
        ) from exc


def task_history_on(
    session: Any, request: TaskRequest, usage: UsageAccess | None = None
) -> dict[str, Any]:
    """Run the `task_history` tool on an open session."""
    run = guarded_runner(session)
    access = UsageAccess() if usage is None else usage

    def execute() -> dict[str, Any]:
        """Resolve the range, then read the task's executions from the right source."""
        start_ms, end_ms, stale = _resolve_window(run, request.plan, account_usage=True)
        params: list[Any] = [
            start_ms,
            end_ms,
            request.database.upper(),
            request.schema.upper(),
            request.task.upper(),
            request.limit + 1,
        ]
        columns = TASK_COLUMNS + (TASK_TEXT_COLUMNS if request.include_text else [])
        if stale:
            source, rows = _read_account_usage(
                run,
                access,
                "TASK_HISTORY",
                lambda src: build_account_usage_task_history_sql(
                    src, request.include_text
                ),
                params,
            )
            freshness, notes = _USAGE_FRESHNESS, [_usage_note("TASK_HISTORY", source)]
        else:
            rows = run(
                build_task_history_sql(request.database, request.include_text), params
            )
            source = f"{request.database}.INFORMATION_SCHEMA.TASK_HISTORY"
            freshness, notes = _FRESHNESS, []
        return _result(
            source=source,
            plan_start_ms=start_ms,
            plan_end_ms=end_ms,
            rows=rows,
            columns=columns,
            limit=request.limit,
            zone=request.zone,
            notes=notes,
            freshness=freshness,
        )

    return _guarded_execution(run, _TASK_PRIVILEGE, execute)


def _execute_grouped(
    run: Runner,
    access: UsageAccess,
    request: QueryRequest,
    start_ms: int,
    end_ms: int,
    stale: bool,
) -> dict[str, Any]:
    """The grouped form of `query_history`: one row per `group_by` value."""
    group_by = request.group_by
    assert group_by is not None
    by_warehouse = request.warehouse is not None
    if stale:
        params: list[Any] = [start_ms, end_ms]
        if request.warehouse is not None:
            params.append(request.warehouse)
        params.append(request.limit + 1)
        source, rows = _read_account_usage(
            run,
            access,
            "QUERY_HISTORY",
            lambda src: build_account_usage_query_grouped_sql(
                src, by_warehouse, group_by
            ),
            params,
        )
        freshness, notes = _USAGE_FRESHNESS, [_usage_note("QUERY_HISTORY", source)]
    else:
        params = []
        if request.warehouse is not None:
            params.append(request.warehouse)
        params += [start_ms, end_ms, request.limit + 1]
        rows = run(build_query_history_grouped_sql(by_warehouse, group_by), params)
        function = "QUERY_HISTORY_BY_WAREHOUSE" if by_warehouse else "QUERY_HISTORY"
        source = f"SNOWFLAKE.INFORMATION_SCHEMA.{function}"
        freshness, notes = _FRESHNESS, []
    result = _result(
        source=source,
        plan_start_ms=start_ms,
        plan_end_ms=end_ms,
        rows=rows,
        columns=QUERY_GROUP_COLUMNS,
        limit=request.limit,
        zone=request.zone,
        notes=notes,
        freshness=freshness,
    )
    result["grouped_by"] = request.group_by
    return result


def query_history_on(
    session: Any, request: QueryRequest, usage: UsageAccess | None = None
) -> dict[str, Any]:
    """Run the `query_history` tool on an open session."""
    run = guarded_runner(session)
    access = UsageAccess() if usage is None else usage
    by_warehouse = request.warehouse is not None
    by_query_id = request.query_id is not None

    def execute() -> dict[str, Any]:
        """Resolve the range, then read the matching queries from the right source."""
        start_ms, end_ms, stale = _resolve_window(run, request.plan, account_usage=True)
        if request.group_by is not None:
            return _execute_grouped(run, access, request, start_ms, end_ms, stale)
        columns = QUERY_COLUMNS + (QUERY_TEXT_COLUMNS if request.include_text else [])
        if stale:
            params: list[Any] = [start_ms, end_ms]
            if request.warehouse is not None:
                params.append(request.warehouse)
            if request.query_id is not None:
                params.append(request.query_id)
            params.append(request.limit + 1)
            source, rows = _read_account_usage(
                run,
                access,
                "QUERY_HISTORY",
                lambda src: build_account_usage_query_history_sql(
                    src, by_warehouse, by_query_id, request.include_text
                ),
                params,
            )
            return _result(
                source=source,
                plan_start_ms=start_ms,
                plan_end_ms=end_ms,
                rows=rows,
                columns=columns,
                limit=request.limit,
                zone=request.zone,
                notes=[_usage_note("QUERY_HISTORY", source)],
                freshness=_USAGE_FRESHNESS,
            )
        params = []
        if request.warehouse is not None:
            params.append(request.warehouse)
        params += [start_ms, end_ms]
        if request.query_id is not None:
            params.append(request.query_id)
        params.append(request.limit + 1)
        rows = run(
            build_query_history_sql(by_warehouse, by_query_id, request.include_text),
            params,
        )
        function = "QUERY_HISTORY_BY_WAREHOUSE" if by_warehouse else "QUERY_HISTORY"
        notes = []
        if not by_warehouse:
            notes.append(
                "QUERY_HISTORY returns the connected user's queries, and other users' only "
                "when the active role holds MONITOR or OPERATE on their warehouse or task, "
                "or MONITOR EXECUTION; pass a warehouse to restrict to it."
            )
        return _result(
            source=f"SNOWFLAKE.INFORMATION_SCHEMA.{function}",
            plan_start_ms=start_ms,
            plan_end_ms=end_ms,
            rows=rows,
            columns=columns,
            limit=request.limit,
            zone=request.zone,
            notes=notes,
        )

    return _guarded_execution(run, _QUERY_PRIVILEGE, execute)


def credits_on(session: Any, request: CreditsRequest) -> dict[str, Any]:
    """Run the `warehouse_credits` tool on an open session."""
    run = guarded_runner(session)
    by_warehouse = request.warehouse is not None

    def execute() -> dict[str, Any]:
        """Resolve the range, then read the hourly metering rows and the range totals."""
        start_ms, end_ms = _resolve_span(run, request.plan)
        params: list[Any] = [start_ms, end_ms]
        if request.warehouse is not None:
            params.append(request.warehouse)
        totals_params = list(params)
        params.append(request.limit + 1)
        rows = run(build_credits_sql(by_warehouse), params)
        totals_row = _row_dict(
            run(build_credits_totals_sql(by_warehouse), totals_params)[0]
        )
        totals = {
            key.lower(): _render_value(totals_row.get(key), request.zone)
            for key in (
                "CREDITS_USED_COMPUTE",
                "CREDITS_USED_CLOUD_SERVICES",
                "CREDITS_USED",
            )
        }
        result = _result(
            source="SNOWFLAKE.INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY",
            plan_start_ms=start_ms,
            plan_end_ms=end_ms,
            rows=rows,
            columns=CREDIT_COLUMNS,
            limit=request.limit,
            zone=request.zone,
            notes=[_CREDITS_NOTE],
        )
        result["totals"] = totals
        return result

    return _guarded_execution(run, _CREDITS_PRIVILEGE, execute)


def warehouse_config_on(
    session: Any, request: WarehouseConfigRequest
) -> dict[str, Any]:
    """Run the `warehouse_config` tool on an open session."""
    run = guarded_runner(session)

    def execute() -> dict[str, Any]:
        """Read the live configuration of the warehouse(s)."""
        now_ms = _snowflake_now_ms(run)
        rows = run(build_warehouse_config_sql(request.warehouse), ())
        return _result(
            source="SHOW WAREHOUSES",
            plan_start_ms=now_ms,
            plan_end_ms=now_ms,
            rows=rows,
            columns=WH_CONFIG_COLUMNS,
            limit=request.limit,
            zone=request.zone,
            notes=[_WH_CONFIG_NOTE],
            freshness=_WH_CONFIG_FRESHNESS,
        )

    return _guarded_execution(run, _WH_CONFIG_PRIVILEGE, execute)


def operator_stats_on(session: Any, request: OperatorStatsRequest) -> dict[str, Any]:
    """Run the `query_operator_stats` tool on an open session."""
    run = guarded_runner(session)

    def execute() -> dict[str, Any]:
        """Read the query's operators; the function carries its own 14-day window."""
        now_ms = _snowflake_now_ms(run)
        rows = run(build_operator_stats_sql(), (request.query_id, request.limit + 1))
        return _result(
            source="GET_QUERY_OPERATOR_STATS",
            plan_start_ms=now_ms,
            plan_end_ms=now_ms,
            rows=rows,
            columns=OPERATOR_COLUMNS,
            limit=request.limit,
            zone=request.zone,
            notes=[_OPERATOR_NOTE],
            freshness=_OPERATOR_FRESHNESS,
            variant_columns=OPERATOR_VARIANT_COLUMNS,
        )

    return _guarded_execution(run, _OPERATOR_PRIVILEGE, execute)


def dynamic_table_refresh_on(
    session: Any, request: DynamicTableRequest, usage: UsageAccess | None = None
) -> dict[str, Any]:
    """Run the `dynamic_table_refresh_history` tool on an open session."""
    run = guarded_runner(session)
    access = UsageAccess() if usage is None else usage
    by_name = request.name is not None
    qualified = (
        f"{request.database}.{request.schema}.{request.name}" if by_name else None
    )

    def execute() -> dict[str, Any]:
        """Resolve the range, then read the refreshes from the right source."""
        start_ms, end_ms, stale = _resolve_window(run, request.plan, account_usage=True)
        columns = DT_COLUMNS + (DT_TEXT_COLUMNS if request.include_text else [])
        if stale:
            params: list[Any] = [start_ms, end_ms]
            if by_name:
                # `plan_dynamic_table_refresh` garantit les trois non-None dès que l'un l'est.
                assert request.database and request.schema and request.name
                params += [
                    request.database.upper(),
                    request.schema.upper(),
                    request.name.upper(),
                ]
            elif request.name_prefix is not None:
                params.append(request.name_prefix.upper())
            params.append(request.limit + 1)
            source, rows = _read_account_usage(
                run,
                access,
                "DYNAMIC_TABLE_REFRESH_HISTORY",
                lambda src: build_account_usage_dynamic_table_sql(
                    src,
                    by_name,
                    request.name_prefix is not None,
                    request.error_only,
                    request.include_text,
                ),
                params,
            )
            freshness, notes = (
                _USAGE_FRESHNESS,
                [_usage_note("DYNAMIC_TABLE_REFRESH_HISTORY", source), _DT_NOTE],
            )
        else:
            params = [start_ms, end_ms]
            if qualified is not None:
                params.append(qualified)
            if request.name_prefix is not None:
                params.append(request.name_prefix)
            params.append(request.limit + 1)
            rows = run(
                build_dynamic_table_refresh_sql(
                    qualified,
                    request.name_prefix,
                    request.error_only,
                    request.include_text,
                ),
                params,
            )
            source = "SNOWFLAKE.INFORMATION_SCHEMA.DYNAMIC_TABLE_REFRESH_HISTORY"
            freshness, notes = _FRESHNESS, [_DT_NOTE]
        return _result(
            source=source,
            plan_start_ms=start_ms,
            plan_end_ms=end_ms,
            rows=rows,
            columns=columns,
            limit=request.limit,
            zone=request.zone,
            notes=notes,
            freshness=freshness,
            variant_columns=DT_VARIANT_COLUMNS,
        )

    return _guarded_execution(run, _DT_PRIVILEGE, execute)


_VISIBLE_NAMES_SHOWN = 20


def _names_of(rows: Sequence[Any]) -> list[str]:
    """The `name` column of `SHOW` rows."""
    return [str(_row_dict(row).get("NAME", "")) for row in rows]


def _check_event_scope(run: Runner, request: EventRequest) -> None:
    """Refuse a database or schema the connection cannot see, before any `EVENT_TABLE` lookup.

    `LIKE` reads `_` and `%` as wildcards, so a row is not proof: the name must match exactly,
    case-insensitively.
    """
    checks: list[tuple[str, str, str, str | None]] = [
        ("database", "DATABASES", request.database, None)
    ]
    if request.schema is not None:
        checks.append(("schema", "SCHEMAS", request.schema, request.database))
    for label, kind, name, database in checks:
        found = _names_of(run(build_visible_names_sql(kind, database, name), ()))
        if any(n.upper() == name.upper() for n in found):
            continue
        visible = sorted(_names_of(run(build_visible_names_sql(kind, database), ())))
        shown = visible[:_VISIBLE_NAMES_SHOWN]
        where = f" in database {database}" if database else ""
        more = (
            f" (first {_VISIBLE_NAMES_SHOWN} of {len(visible)})"
            if len(visible) > _VISIBLE_NAMES_SHOWN
            else ""
        )
        raise PcSnowError(
            f"The {label} {name!r} is not visible to the connected role{where}: it does not "
            f"exist, or the role cannot see it. Visible {label}s{more}: "
            f"{', '.join(shown) if shown else 'none'}. Ask the user which one they mean; "
            "do not guess."
        )


def _resolve_event_table(
    run: Runner, request: EventRequest
) -> tuple[str, str, list[str]]:
    """The table `EVENT_TABLE` names at the nearest level: schema, database, then account.

    Returns the table, the level that gave it and the levels Snowflake refused for lack of a
    privilege. A refused level is skipped; when every level was refused, the last refusal is
    raised as it came. Any other failure (connection, session, other SQL error) is raised at once.
    """
    levels = [
        level
        for level in _EVENT_LEVELS
        if level != "SCHEMA" or request.schema is not None
    ]
    skipped: list[str] = []
    last_refusal: Exception | None = None
    for level in levels:
        sql = build_event_parameter_sql(level, request.database, request.schema)
        try:
            rows = run(sql, ())
        except PcSnowError:
            raise
        except Exception as exc:
            if not _is_privilege_error(exc):
                raise
            skipped.append(level)
            last_refusal = exc
            continue
        for row in rows:
            found = _row_dict(row)
            value = str(found.get("VALUE") or "").strip()
            if str(found.get("KEY", "")).upper() == "EVENT_TABLE" and value:
                if not _TABLE_NAME.fullmatch(value):
                    raise PcSnowError(
                        f"EVENT_TABLE at the {level} level is {value!r}, which is not a plain "
                        "database.schema.table name; it is not read."
                    )
                return value, level, skipped
    if last_refusal is not None and len(skipped) == len(levels):
        raise last_refusal
    asked = (
        f"{request.database}.{request.schema}" if request.schema else request.database
    )
    skipped_note = (
        f" Levels skipped, refused or not found (Snowflake error 2003 covers both): "
        f"{', '.join(skipped)}."
        if skipped
        else ""
    )
    raise PcSnowError(
        f"No EVENT_TABLE value found for {asked} or its account.{skipped_note} Ask the user "
        "which database holds their telemetry, or whether an event table is set up at all."
    )


def event_records_on(session: Any, request: EventRequest) -> dict[str, Any]:
    """Run the `event_table_records` tool on an open session."""
    run = guarded_runner(session)
    by_query_id = request.query_id is not None

    def execute() -> dict[str, Any]:
        """Resolve the range, then the event table, then read its records."""
        start_ms, end_ms = _resolve_span(run, request.plan)
        _check_event_scope(run, request)
        table, level, skipped = _resolve_event_table(run, request)
        params: list[Any] = [table, start_ms, end_ms]
        if request.query_id is not None:
            params.append(request.query_id)
        params.append(request.limit + 1)
        rows = run(build_event_records_sql(by_query_id, request.include_text), params)
        columns = EVENT_COLUMNS + (EVENT_TEXT_COLUMNS if request.include_text else [])
        notes = [_EVENT_NOTE]
        if skipped:
            notes.append(
                f"SHOW PARAMETERS was refused or not found at the {', '.join(skipped)} "
                "level (Snowflake error 2003 covers both); it was skipped."
            )
        return _result(
            source=f"{table} (EVENT_TABLE resolved at the {level} level)",
            plan_start_ms=start_ms,
            plan_end_ms=end_ms,
            rows=rows,
            columns=columns,
            limit=request.limit,
            zone=request.zone,
            notes=notes,
            freshness=_EVENT_FRESHNESS,
        )

    return _guarded_execution(run, _EVENT_PRIVILEGE, execute)


# ── Connection ───────────────────────────────────────────────────────────────────────────────────


def snowflake_config_dir(environ: Mapping[str, str]) -> Path:
    """The directory holding `connections.toml` and `config.toml`, as the connector resolves it."""
    home = environ.get("SNOWFLAKE_HOME")
    if home:
        return Path(home).expanduser()
    legacy = Path("~/.snowflake").expanduser()
    if legacy.exists():
        return legacy
    mac = Path("~/Library/Application Support/snowflake").expanduser()
    if mac.exists():
        return mac
    xdg = environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path("~/.config").expanduser()
    return base / "snowflake"


def _read_toml(path: Path) -> dict[str, Any]:
    """A TOML file as a mapping; a missing or unreadable file is an empty one."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


@dataclass(frozen=True)
class Profiles:
    """The connection profiles on this machine and whether one of them is the default."""

    names: list[str]
    has_default: bool


def configured_profiles(environ: Mapping[str, str]) -> Profiles:
    """Read the profile names from `connections.toml` and `config.toml`.

    A default exists when `SNOWFLAKE_DEFAULT_CONNECTION_NAME` or `default_connection_name` is set,
    or when a profile is literally named `default` — the three ways the connector picks one.
    """
    directory = snowflake_config_dir(environ)
    connections = _read_toml(directory / "connections.toml")
    config = _read_toml(directory / "config.toml")
    names: list[str] = []
    for source in (connections, config.get("connections", {})):
        if isinstance(source, dict):
            names += [
                key
                for key, value in source.items()
                if isinstance(value, dict) and key not in names
            ]
    has_default = bool(
        environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME")
        or config.get("default_connection_name")
        or "default" in names
    )
    return Profiles(names, has_default)


class Connection:
    """The process's one Snowflake session, opened on the first tool call and kept after that.

    `builder_factory` returns a fresh Snowpark session builder; nothing here imports Snowpark.
    """

    def __init__(
        self,
        builder_factory: Callable[[], Any],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Hold the builder factory; no session is opened and nothing is asked."""
        self._builder_factory = builder_factory
        self._environ = os.environ if environ is None else environ
        self._session: Any = None
        self._chosen_profile: str | None = None
        self.usage = UsageAccess()
        self._lock = threading.Lock()
        self._opening = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        """Whether a session has been opened."""
        return self._session is not None

    async def session(self, ask: ProfileAsker | None) -> Any:
        """Return the open session, opening it (and asking for a profile if needed) first."""
        async with self._opening:
            if self._session is not None:
                return self._session
            profile = await self._profile(ask)
            return await asyncio.to_thread(self._open, profile)

    async def _profile(self, ask: ProfileAsker | None) -> str | None:
        """The profile to open: `None` for the connector's default, else the user's choice."""
        if self._chosen_profile:
            return self._chosen_profile
        found = configured_profiles(self._environ)
        names = found.names
        if len(names) < 2 or found.has_default:
            return None
        answer = await ask(names) if ask is not None else None
        if answer not in names:
            raise PcSnowError(
                f"Several Snowflake connection profiles are configured ({', '.join(names)}) "
                "and none is the default. Set SNOWFLAKE_DEFAULT_CONNECTION_NAME to the profile "
                "to use, or declare default_connection_name in config.toml. "
                "No session was opened."
            )
        self._chosen_profile = answer
        return answer

    def _open(self, profile: str | None) -> Any:
        """Open the session, then activate secondary roles once."""
        with self._lock:
            if self._session is not None:
                return self._session
            try:
                builder = self._builder_factory()
            except ImportError as exc:
                raise PcSnowError(
                    "snowflake-snowpark-python is not importable here; run the server "
                    "through `uv run --script`, which installs it."
                ) from exc
            if profile:
                builder = builder.config("connection_name", profile)
            try:
                session = builder.getOrCreate()
            except Exception as exc:
                raise PcSnowError(
                    f"Could not open a Snowflake session: {type(exc).__name__}: "
                    f"{_error_text(exc)}. Check the default connection "
                    "(default_connection_name in config.toml, or "
                    "SNOWFLAKE_DEFAULT_CONNECTION_NAME)."
                ) from exc
            try:
                guarded_runner(session)(_SECONDARY_ROLES_SQL, ())
            except Exception as exc:
                try:
                    session.close()
                except Exception:
                    # La session déjà inutilisable : l'échec d'origine est ce qu'on rapporte.
                    pass
                raise PcSnowError(
                    f"`{_SECONDARY_ROLES_SQL}` failed when opening the session: "
                    f"{type(exc).__name__}: {_error_text(exc)}. No session was kept."
                ) from exc
            self._session = session
            return session

    def reset(self) -> None:
        """Forget the open session; the next `session()` call opens a fresh one."""
        with self._lock:
            session = self._session
            self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                # La session est déjà morte : c'est précisément pourquoi on la remplace.
                pass


async def _run_with_reconnect(
    connection: Connection,
    ask: ProfileAsker | None,
    action: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """Run `action` on the session; on a dead session, open a fresh one and retry once."""
    session = await connection.session(ask)
    try:
        return await asyncio.to_thread(action, session)
    except Exception as exc:
        # `_guarded_execution` a déjà enveloppé l'erreur dans un PcSnowError ; le texte d'origine
        # de Snowflake reste dans son message, donc la détection s'appuie aussi sur le texte.
        if not _is_session_expired(exc):
            raise
        connection.reset()
        session = await connection.session(ask)
        return await asyncio.to_thread(action, session)


async def task_history(
    connection: Connection,
    ask: ProfileAsker | None,
    request: TaskRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `task_history` off the event loop."""
    return await _run_with_reconnect(
        connection, ask, lambda s: task_history_on(s, request, connection.usage)
    )


async def query_history(
    connection: Connection,
    ask: ProfileAsker | None,
    request: QueryRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `query_history` off the event loop."""
    return await _run_with_reconnect(
        connection, ask, lambda s: query_history_on(s, request, connection.usage)
    )


async def warehouse_credits(
    connection: Connection,
    ask: ProfileAsker | None,
    request: CreditsRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `warehouse_credits` off the event loop."""
    return await _run_with_reconnect(connection, ask, lambda s: credits_on(s, request))


async def event_table_records(
    connection: Connection,
    ask: ProfileAsker | None,
    request: EventRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `event_table_records` off the event loop."""
    return await _run_with_reconnect(
        connection, ask, lambda s: event_records_on(s, request)
    )


async def query_operator_stats(
    connection: Connection,
    ask: ProfileAsker | None,
    request: OperatorStatsRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `query_operator_stats` off the event loop."""
    return await _run_with_reconnect(
        connection, ask, lambda s: operator_stats_on(s, request)
    )


async def dynamic_table_refresh_history(
    connection: Connection,
    ask: ProfileAsker | None,
    request: DynamicTableRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `dynamic_table_refresh_history` off the event loop."""
    return await _run_with_reconnect(
        connection,
        ask,
        lambda s: dynamic_table_refresh_on(s, request, connection.usage),
    )


async def warehouse_config(
    connection: Connection,
    ask: ProfileAsker | None,
    request: WarehouseConfigRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `warehouse_config` off the event loop."""
    return await _run_with_reconnect(
        connection, ask, lambda s: warehouse_config_on(s, request)
    )
