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
- Every emitted statement is a fixed `SELECT` or `SHOW` with bound values, apart from the single
  `USE SECONDARY ROLES ALL` issued when the session opens.
- Output is bounded: at most `MAX_LIMIT` rows, text fields cut at `MAX_TEXT_CHARS`, free text
  (query text, error messages) only when the caller sets `include_text`.

The connection is the connector's own default (`default_connection_name` in `config.toml`, or
`SNOWFLAKE_DEFAULT_CONNECTION_NAME`); the server adds a profile name only after the user chose one.

Errors: on mcp 2.2.0 only a `ToolError` carries its message to the model; any other exception
reaches it as `Error executing tool <name>` with no cause. The server turns `PcSnowError` into a
`ToolError`, and any new tool that fails on purpose must raise one of the two.
"""

from __future__ import annotations

import asyncio
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
_PRIVILEGE_TEXT = re.compile(
    r"insufficient privileges|not authorized|permission denied|access denied",
    re.IGNORECASE,
)
_PRIVILEGE_ERROR_CODES = frozenset({2003, 3001})

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
) -> RangePlan:
    """Validate the range arguments; no statement is emitted for a refused range."""
    if last_minutes is not None and (start or end):
        raise PcSnowError(
            "last_minutes cannot be combined with start or end: give either "
            "start and end, or last_minutes."
        )
    if last_minutes is not None:
        if last_minutes < 1:
            raise PcSnowError("last_minutes must be at least 1.")
        if last_minutes > MAX_RANGE_MINUTES:
            raise PcSnowError(
                f"last_minutes={last_minutes} exceeds the {HISTORY_DAYS}-day limit of "
                f"INFORMATION_SCHEMA functions (at most {MAX_RANGE_MINUTES} minutes)."
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
        if last - first > timedelta(days=HISTORY_DAYS):
            raise PcSnowError(
                f"The range {start} .. {end} is longer than the {HISTORY_DAYS}-day limit of "
                "INFORMATION_SCHEMA functions. Ask the user for a shorter range."
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
    """A validated `query_history` call."""

    query_id: str | None
    warehouse: str | None
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
    plan = plan_range(start, end, last_minutes, zone, accepts_query_id=False)
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
    environ: Mapping[str, str] | None = None,
) -> QueryRequest:
    """Validate a `query_history` call: a `query_id` or a warehouse, then range, then limit."""
    if not query_id and not warehouse:
        raise _ask_scope("query_history", ["query_id or warehouse"])
    if query_id and not _QUERY_ID.fullmatch(query_id.strip()):
        raise PcSnowError(f"query_id={query_id!r} is not a query id (a UUID).")
    zone = user_zone(environ)
    plan = plan_range(
        start,
        end,
        last_minutes,
        zone,
        accepts_query_id=True,
        query_id_given=bool(query_id),
    )
    return QueryRequest(
        query_id=query_id.strip().lower() if query_id else None,
        warehouse=_identifier("warehouse", warehouse).upper() if warehouse else None,
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
    if value is None or isinstance(value, bool | int | float | str):
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
    rows: Sequence[Any], columns: Sequence[str], zone: UserZone
) -> tuple[list[dict[str, Any]], list[FieldTruncation]]:
    """Keep only the allowed columns, render values, cut text and record each cut."""
    wanted = set(columns)
    shaped: list[dict[str, Any]] = []
    cuts: list[FieldTruncation] = []
    for index, row in enumerate(rows):
        out: dict[str, Any] = {}
        for column, value in _row_dict(row).items():
            if column not in wanted:
                continue
            rendered = _render_value(value, zone)
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
) -> dict[str, Any]:
    """The common result envelope: source, freshness, resolved range, bounded rows."""
    kept = list(rows)[:limit]
    shaped, cuts = _shape_rows(kept, columns, zone)
    return {
        "source": source,
        "fraicheur": _FRESHNESS,
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


def _is_privilege_error(exc: BaseException) -> bool:
    """Whether Snowflake refused the call for lack of a privilege."""
    code = getattr(exc, "sql_error_code", None) or getattr(exc, "errno", None)
    return code in _PRIVILEGE_ERROR_CODES or bool(
        _PRIVILEGE_TEXT.search(_error_text(exc))
    )


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


def _resolve_span(run: Runner, plan: RangePlan) -> tuple[int, int]:
    """Start and end in epoch ms, read against Snowflake's clock; stale ranges are refused."""
    now_ms = _snowflake_now_ms(run)
    if plan.start_ms is not None and plan.end_ms is not None:
        if plan.start_ms > now_ms:
            raise PcSnowError(
                "The range starts in the future by Snowflake's clock, so it holds no history. "
                "Ask the user for a range that has already begun."
            )
        if plan.start_ms < now_ms - MAX_RANGE_MINUTES * 60_000:
            raise PcSnowError(
                f"The range starts more than {HISTORY_DAYS} days ago, beyond what "
                "INFORMATION_SCHEMA functions keep. Ask the user for a more recent range."
            )
        return plan.start_ms, plan.end_ms
    minutes = plan.minutes if plan.minutes is not None else MAX_RANGE_MINUTES
    return now_ms - minutes * 60_000, now_ms


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


def task_history_on(session: Any, request: TaskRequest) -> dict[str, Any]:
    """Run the `task_history` tool on an open session."""
    run = guarded_runner(session)

    def execute() -> dict[str, Any]:
        """Resolve the range, then read the task's executions."""
        start_ms, end_ms = _resolve_span(run, request.plan)
        rows = run(
            build_task_history_sql(request.database, request.include_text),
            [
                start_ms,
                end_ms,
                request.database.upper(),
                request.schema.upper(),
                request.task.upper(),
                request.limit + 1,
            ],
        )
        columns = TASK_COLUMNS + (TASK_TEXT_COLUMNS if request.include_text else [])
        return _result(
            source=f"{request.database}.INFORMATION_SCHEMA.TASK_HISTORY",
            plan_start_ms=start_ms,
            plan_end_ms=end_ms,
            rows=rows,
            columns=columns,
            limit=request.limit,
            zone=request.zone,
            notes=[],
        )

    return _guarded_execution(run, _TASK_PRIVILEGE, execute)


def query_history_on(session: Any, request: QueryRequest) -> dict[str, Any]:
    """Run the `query_history` tool on an open session."""
    run = guarded_runner(session)
    by_warehouse = request.warehouse is not None
    by_query_id = request.query_id is not None

    def execute() -> dict[str, Any]:
        """Resolve the range, then read the matching queries."""
        start_ms, end_ms = _resolve_span(run, request.plan)
        params: list[Any] = []
        if by_warehouse:
            params.append(request.warehouse)
        params += [start_ms, end_ms]
        if by_query_id:
            params.append(request.query_id)
        params.append(request.limit + 1)
        rows = run(
            build_query_history_sql(by_warehouse, by_query_id, request.include_text),
            params,
        )
        columns = QUERY_COLUMNS + (QUERY_TEXT_COLUMNS if request.include_text else [])
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


async def task_history(
    connection: Connection,
    ask: ProfileAsker | None,
    request: TaskRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `task_history` off the event loop."""
    session = await connection.session(ask)
    return await asyncio.to_thread(task_history_on, session, request)


async def query_history(
    connection: Connection,
    ask: ProfileAsker | None,
    request: QueryRequest,
) -> dict[str, Any]:
    """Open the session if needed, then run `query_history` off the event loop."""
    session = await connection.session(ask)
    return await asyncio.to_thread(query_history_on, session, request)
