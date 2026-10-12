# Snowflake system views — what the pc-snow observability tools read

Covers: the two tools of the `pc-snow` MCP server (`task_history`, `query_history`), the `INFORMATION_SCHEMA`
functions behind them, how to read their columns, and the interview to run before calling either.
Facts below come from the Snowflake documentation pages of `TASK_HISTORY` and `QUERY_HISTORY`
(Information Schema); a fact marked *measured* was observed on a real account through these tools,
and one marked *not measured* is announced by the tool but has no observation behind it. For a
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

Ask for what is missing, in one question, and nothing else:

1. Which task (`database`, `schema`, `task`) or which query (`query_id`, or `warehouse`)?
2. Which period? Either two dates (`start`, `end`) or "the last N minutes" (`last_minutes`).
   A date without an offset is read in the user's timezone (`PC_SNOW_TIMEZONE`, else the machine's
   zone); an offset is honoured as written. The Snowflake session's own `TIMEZONE` is never used.
   A wall-clock time that does not exist or occurs twice in a daylight-saving change is refused —
   ask for an explicit offset. A range that starts in the future is refused.
3. Does the user need the text (`include_text=true`)? Query text, error messages and task return
   values can hold literals and leave the machine for a cloud model: ask before turning it on.

Also: `limit` is 1 to 200 rows, 50 by default; a period over 7 days is refused (the functions keep
7 days) — ask for a shorter one rather than splitting it silently. `last_minutes` is at most 10079,
one minute under 7 days: the window a `query_id` alone searches.

---

## task_history

`<database>.INFORMATION_SCHEMA.TASK_HISTORY` — the executions of one task, newest first.

- **What it returns**: one row per execution (scheduled, running or finished) whose scheduled time
  falls in the period, in descending `SCHEDULED_TIME` order.
- **Freshness**: 7 days of history, an error beyond. The tool reports it as near real time (*not
  measured*); older data is in the `ACCOUNT_USAGE` view, which this tool does not read.
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
- **Freshness**: 7 days of history, an error beyond. Reported as near real time (*not measured*).
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
