# Snowflake system views — what the pc-snow observability tools read

Covers: the tools of the `pc-snow` MCP server (`task_history`, `query_history`, `warehouse_credits`,
`event_table_records`), the `INFORMATION_SCHEMA` functions and the event table behind them, how to
read their columns, and the interview to run before calling them. Facts below come from the
Snowflake documentation pages of `TASK_HISTORY`, `QUERY_HISTORY` and `WAREHOUSE_METERING_HISTORY`
(Information Schema), of the `ACCOUNT_USAGE` views
(<https://docs.snowflake.com/en/sql-reference/account-usage>, and
<https://docs.snowflake.com/en/user-guide/ui-snowsight-activity> for the role that covers query
history) and of event tables; a fact marked *measured* was observed on a real account through these
tools, and one marked *not measured* is announced by the tool but has no observation behind it. For a
signature or limit you are unsure of, query `context7` (`libraryId: /websites/snowflake_en`).

The tools are fixed queries with bound arguments: no SQL goes in, only a scope and a range. Each
result carries `source`, `fraicheur`, the resolved `range` (start, end, timezone, offset), `rows`,
`truncated` and, when a text field was cut, `truncated_fields`. Every timestamp carries its offset,
which is authoritative: `timezone` is the `PC_SNOW_TIMEZONE` name when the variable is set, else the
system's abbreviation (such as `CEST`), which is not an IANA name.

---

## Interview protocol — ask before calling

A scope is never guessed, never taken from the session's current database, schema or warehouse,
and never searched for with a discovery query. A call without it is refused; the parameters below
are what the user must supply, in their own words, before the call.

| Tool | Scope to ask for | Range to ask for |
|---|---|---|
| `task_history` | `database`, `schema`, `task` | `start` and `end`, or `last_minutes` — always |
| `query_history` | `query_id`, or `warehouse` | `start` and `end`, or `last_minutes` — optional with a `query_id` |
| `warehouse_credits` | `warehouse`, or `all_warehouses=true` — one of the two, never both | `start` and `end`, or `last_minutes` — always |
| `event_table_records` | `database`; `schema` and `query_id` optional | `start` and `end`, or `last_minutes` — always, even with a `query_id` |

Ask for what is missing, in one question, and nothing else:

1. Which task (`database`, `schema`, `task`), which query (`query_id`, or `warehouse`) or which
   warehouse's credits (`warehouse`, or all of them with `all_warehouses=true`) or which
   telemetry (`event_table_records`: the `database`, the `schema` if any, a `query_id` if known)?
2. Which period? Either two dates (`start`, `end`) or "the last N minutes" (`last_minutes`).
   A date without an offset is read in the user's timezone (`PC_SNOW_TIMEZONE`, else the machine's
   zone); an offset is honoured as written. The Snowflake session's own `TIMEZONE` is never used.
   A wall-clock time that does not exist or occurs twice in a daylight-saving change is refused —
   ask for an explicit offset. A range that starts in the future is refused.
3. Does the user need the text (`include_text=true`)? Query text, error messages, task return
   values, log messages and span attributes can hold literals and leave the machine for a cloud model: ask before turning it on.

Also: `limit` is 1 to 200 rows, 50 by default. `warehouse_credits` and `event_table_records` refuse
a period over 7 days — ask for a shorter one rather than splitting it silently — and their
`last_minutes` is at most 10079, one minute under 7 days. For `query_history` that is also the
window a `query_id` alone searches.
`task_history` and `query_history` accept a period that starts earlier, up to 365 days
(`last_minutes` at most 525600), and read the `ACCOUNT_USAGE` view for it; see *Beyond 7 days* below.

---

## Beyond 7 days — `task_history` and `query_history`

A range that starts more than 7 days before Snowflake's clock, or a `last_minutes` over 10079, is read
from the `ACCOUNT_USAGE` view instead of the function. The result's `source` names the view and
`fraicheur` says it is delayed (*différée*); the latest executions may be missing. The view is
account-wide: the scope is a bound `WHERE`, and the range filters on `SCHEDULED_TIME` (tasks) or
`END_TIME` (queries). The view keeps 365 days; a longer range is refused. The exact latency comes
from the documentation and is *not measured*. The time columns are `TIMESTAMP_LTZ`
(*measured* by `DESCRIBE VIEW`, on one test account, one role): `START_TIME` and `END_TIME` of
`QUERY_HISTORY` are `TIMESTAMP_LTZ(6)`, `SCHEDULED_TIME` of `TASK_HISTORY` is `TIMESTAMP_LTZ(3)`.

Access is a database role per view, not a role the tool can choose, and one role does not cover
both views:

| View | Database role | Status |
|---|---|---|
| `TASK_HISTORY` | `SNOWFLAKE.USAGE_VIEWER` | *measured*: readable after the grant (289 rows over 10 days), on one test account, one role |
| `QUERY_HISTORY` | `SNOWFLAKE.GOVERNANCE_VIEWER` | *measured*: refused (error 2003) with only `USAGE_VIEWER`; after the `GOVERNANCE_VIEWER` grant, 29583 rows over 10 days; one test account, one role |

The server runs one trial `SELECT` per view, the first time a call needs that view, and keeps each
answer in the memory of the process, never on disk: a grant made later is seen after the server
restarts or reconnects. A refusal names `GRANT DATABASE ROLE SNOWFLAKE.<role> TO ROLE <role>` for
the view that was refused — never the other view's role — which `ACCOUNTADMIN` runs. A range inside
7 days never needs it. A refused object of the `SNOWFLAKE` database comes back as error 2003
(*does not exist or not authorized*), not as an empty result (*measured*).

The view reads are bounded by `LIMIT`, not by what the view scans: ask for the narrowest range that
answers the question.

---

## task_history

`<database>.INFORMATION_SCHEMA.TASK_HISTORY` — the executions of one task, newest first.

- **What it returns**: one row per execution (scheduled, running or finished) whose scheduled time
  falls in the period, in descending `SCHEDULED_TIME` order.
- **Freshness**: 7 days of history from the function, reported as near real time (*not measured*);
  a range that starts earlier is read from `SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY` (*Beyond 7 days*).
- **Scope arguments**: `DATABASE_NAME`, `SCHEMA_NAME` and `TASK_NAME` are function arguments,
  applied *before* `RESULT_LIMIT` (1 to 10 000, default 100; the tool always asks for 10 000), so a
  task is found even when other tasks have thousands of executions. `SCHEMA_NAME` requires
  `DATABASE_NAME`; `TASK_NAME` is case-insensitive and takes the unqualified name. Accepted by the
  live account with these three arguments (*measured*); the filtering itself is the documented
  behaviour, *not measured* (the test account had no execution in 7 days).
- **Privileges**: OWNERSHIP, MONITOR or OPERATE on the task, the global MONITOR EXECUTION, or
  ACCOUNTADMIN, plus USAGE on the database and schema — without USAGE, `DATABASE_NAME` and
  `SCHEMA_NAME` come back NULL. A refusal is returned with the missing privilege and the primary
  and secondary roles of the session.

Key columns:

| Column | Reading |
|---|---|
| `STATE` | `SCHEDULED`, `EXECUTING`, `SUCCEEDED`, `FAILED` (a timeout is `FAILED`), `FAILED_AND_AUTO_SUSPENDED` (will not run again until resumed), `CANCELLED`, `SKIPPED` (the `WHEN` condition was false: no warehouse resumed, no SQL run) |
| `SCHEDULED_TIME` | when the execution was due to start; the sort key |
| `QUERY_START_TIME` | NULL while the scheduled time is in the future or the run has not started |
| `COMPLETED_TIME` | NULL while the scheduled time is in the future or the task is still running |
| `SCHEDULED_FROM` | `SCHEDULE`, `EXECUTE_TASK`, `MANUAL RETRY`, `AUTOMATIC RETRY` or `TRIGGER` (stream with new data); a child task shows its root's value |
| `QUERY_ID` | the statement the task ran, filled only once the task has started — read it with `query_history` |
| `GRAPH_RUN_GROUP_ID` | groups the task runs of one graph run |
| `ERROR_CODE` | always returned; `ERROR_MESSAGE`, `RETURN_VALUE` (the value set by the predecessor task) and `QUERY_TEXT` only with `include_text=true` |

Pitfalls:

- A name that is not a plain identifier (quotes, spaces, `;`) is refused, not escaped.
- When the period reaches now, rows with `STATE` `SCHEDULED` and an empty `QUERY_ID` are
  executions not yet started — the function offers `query_id IS NOT NULL` to drop them, the tool
  does not.
- A database in which the function does not resolve answered `Invalid identifier
  <db>.INFORMATION_SCHEMA.TASK_HISTORY` on the test account (*measured*, cause not established);
  the message is returned as is.
- Empty rows over a period mean no execution was recorded for that task in it — not that the task
  is healthy or exists.

---

## query_history

`SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY` and `QUERY_HISTORY_BY_WAREHOUSE` — queries by id or by
warehouse, newest first.

- **What it returns**: one row per query whose *end time* falls in the period. The tool always
  sends an end bound, so a query still running is not returned.
- **Freshness**: 7 days of history from the function, reported as near real time (*not measured*);
  a range that starts earlier is read from `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` (*Beyond 7 days*).
- **Visibility**: both functions return the connected user's queries, and any user's queries when
  the active role holds MONITOR or OPERATE on the warehouse where they ran, or on the task, or
  MONITOR EXECUTION on the account. A query that does not appear is not necessarily absent: the
  role may not be allowed to see it.
- **Which function**: with a `warehouse`, `QUERY_HISTORY_BY_WAREHOUSE` restricts to queries run on
  that warehouse (the name is matched as an unquoted identifier, so a mixed-case warehouse cannot
  be addressed). Without one, `QUERY_HISTORY`. Both worked live by warehouse and by id (*measured*).
- **`RESULT_LIMIT`** (1 to 10 000, default 100): applied *before* any `WHERE`. The tool always asks
  for 10 000 and filters on `QUERY_ID` afterwards, so a `query_id` is found even when more than 100
  queries ran since. With a `query_id` and no range, the whole 7-day window is searched.

Key columns:

| Column | Reading |
|---|---|
| `EXECUTION_STATUS` | `resuming_warehouse`, `running`, `queued`, `blocked`, `success`, `failed_with_error`, `failed_with_incident` |
| `START_TIME`, `END_TIME` | the range filters on `END_TIME`; both rendered in the user's timezone; a running query shows the 1970 epoch as `END_TIME` |
| `TOTAL_ELAPSED_TIME` | milliseconds |
| `QUEUED_OVERLOAD_TIME` | milliseconds queued because the warehouse was overloaded by the current workload — a sizing signal |
| `BYTES_SCANNED`, `ROWS_PRODUCED` | volume read and returned |
| `WAREHOUSE_NAME`, `WAREHOUSE_SIZE` | the warehouse the query ran on, empty when none |
| `QUERY_TAG` | the session's `QUERY_TAG` at run time |
| `ERROR_CODE` | always returned; `ERROR_MESSAGE` and `QUERY_TEXT` only with `include_text=true` |

Pitfalls:

- A cancelled query is identified by its `ERROR_MESSAGE` text (`SQL execution canceled`), not by
  `EXECUTION_STATUS` — it needs `include_text=true` to be told apart.
- The functions take `TIMESTAMP_LTZ` arguments, whose reading depends on the session `TIMEZONE`.
  The tools bind epoch milliseconds through `TO_TIMESTAMP_LTZ(?, 3)`, so the same interval selects
  the same queries under any session timezone (*measured*: identical ids under two session
  timezones).
- `last_minutes` is resolved by Snowflake's clock, not the machine's; the resolved range is in the
  result.
- Text fields are cut at 200 characters; `truncated_fields` lists each cut with the original length.
- Whether a query launched by a task appears through `QUERY_HISTORY` follows the visibility rule
  above (MONITOR or OPERATE on the task); *not measured* — the test account has no task run.

---

## warehouse_credits

`SNOWFLAKE.INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY` — credits consumed per warehouse and per
hour, newest first.

- **What it returns**: one row per warehouse and hour in the period, with `CREDITS_USED_COMPUTE`,
  `CREDITS_USED_CLOUD_SERVICES` and their sum `CREDITS_USED`; `START_TIME` and `END_TIME` bound the
  hour and are rendered in the user's timezone.
- **Scope**: a `warehouse` (matched as an unquoted identifier, upper-cased), or `all_warehouses=true`
  which calls the function without a name. Omitting both is refused with an instruction to ask the
  user; giving both is refused. Omitting the name never means all.
- **Source**: the `INFORMATION_SCHEMA` function, not `ACCOUNT_USAGE` (latency of up to 3 hours, see
  `references/costs.md`); it does not need imported privileges on the `SNOWFLAKE` database. Called
  without a database prefix, it did not resolve on the test account (*measured*: `Invalid identifier
  INFORMATION_SCHEMA.WAREHOUSE_METERING_HISTORY`); the tool qualifies it with `SNOWFLAKE`.
- **Privileges**: ACCOUNTADMIN, or a role granted the global `MONITOR USAGE`. A refusal from
  Snowflake is returned with `MONITOR USAGE` named and the active roles. **Without the privilege the
  function returned no row and no error** on the test account (*measured*: role without `MONITOR
  USAGE`, three warehouses, 0 rows over 7 days). The result's `notes` says so: an empty result is not
  proof that nothing was consumed.

Pitfalls:

- The 7-day range limit of the common contract applies; the function itself keeps more, which the
  tool does not expose.
- `limit` caps hourly rows, not warehouses: 200 rows cover about 8 days of one warehouse, or one
  day of eight. Narrow the range or name a warehouse before raising it.

---

## event_table_records

The event table that `EVENT_TABLE` designates for the scope, read directly — logs, spans and events
of procedures, functions and tasks, newest first. The caller never names the table.

- **Resolution**: `SHOW PARAMETERS LIKE 'event_table'` at the schema (when a `schema` is given),
  then at the database, then at the account; the first level that returns a value wins, and the
  table it names is read. A level Snowflake refuses is skipped and named in `notes`; if all are
  refused, the last refusal is returned. The resolved table, with its level, is the `source`.
- **Measured** on one test account, one role, in two states. Before any binding:
  `SHOW PARAMETERS LIKE 'event_table'` IN ACCOUNT and IN DATABASE both returned the
  Snowflake-provided default table `snowflake.telemetry.events`, with `level` empty. After
  `ALTER DATABASE <db> SET EVENT_TABLE = <table>`: IN DATABASE `<db>` returned that table with
  `level` = `DATABASE`, and IN ACCOUNT still the default. IN SCHEMA of one schema of that database
  returned no row and no error, in both states. `ALTER SCHEMA ... SET EVENT_TABLE` was not
  attempted; whether another account holds a schema-level value is not established. See
  `references/telemetry.md`.
- **Visibility**: before the lookup the tool checks that the `database` (and the `schema`) is
  visible to the connected role, with `SHOW TERSE DATABASES|SCHEMAS`, because error 2003 (*does not
  exist or not authorized*) cannot tell a misspelling from a missing grant. A name that is not
  visible is refused with the visible names, up to 20, and the question to ask the user.
- **Measured** on one real event table record: the columns `RECORD_TYPE`, `RECORD['severity_text']`,
  `RESOURCE_ATTRIBUTES['snow.executable.name']`, `RESOURCE_ATTRIBUTES['snow.query.id']`, `VALUE` and
  `RECORD_ATTRIBUTES` exist; `TIMESTAMP`, `START_TIMESTAMP` and `OBSERVED_TIMESTAMP` are
  `TIMESTAMP_NTZ(9)`; one `LOG` record returned severity `INFO` and the executable name of the
  logging procedure.
- **Columns**: `TIMESTAMP` and `START_TIMESTAMP` are `TIMESTAMP_NTZ` in UTC and are rendered in the
  user's timezone with their offset; a range given with an offset selects the same records
  whatever the session's `TIMEZONE`. `SEVERITY` is `RECORD['severity_text']`, `EXECUTABLE_NAME` is
  `RESOURCE_ATTRIBUTES['snow.executable.name']` and `QUERY_ID` is
  `RESOURCE_ATTRIBUTES['snow.query.id']`, the filter a `query_id` applies. `MESSAGE` (the log body,
  `VALUE`) and `ATTRIBUTES` (`RECORD_ATTRIBUTES`) hold text written by user code and come back only
  with `include_text=true`, cut at 200 characters.
- **Pitfalls**: an empty result is not proof that nothing was recorded — the table holds only what
  `LOG_LEVEL` and `TRACE_LEVEL` let through. A missing privilege on the resolved table came back as
  an error (2003, *measured* on the default table), not as an empty result; the empty-instead-of-error
  behaviour is measured for `warehouse_credits` only. Records are written asynchronously and may lag the
  event. The 7-day range limit of the common contract applies although an event table keeps more.
