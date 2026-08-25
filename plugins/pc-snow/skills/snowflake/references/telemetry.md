# Snowflake Telemetry & Event Table

Reference: `snowflake-telemetry-python` package + Snowflake event table infrastructure.
Primary consumption interface: **Snowsight Monitoring UI** — not raw SQL on EVENT_TABLE.

> **⚠ Active development area — track releases closely.**
> `snowflake-telemetry-python` is pre-v1 (0.x) — no API stability guarantee. Breaking changes are expected.
> Snowflake acquired Observe (log/trace/metric analytics platform) in 2024. The integration is actively being built into the Snowflake observability stack — significant feature additions expected throughout 2026 (OBSERVE platform, `add_metric()`, enhanced event table querying, cross-account telemetry).
> Given the pace of change, query `context7` (`libraryId: /websites/snowflake_en`, `logging-tracing/`
> section) before trusting training data on this topic.

---

## How it works — automatic capture

Activating `LOG_LEVEL`, `TRACE_LEVEL`, or `METRIC_LEVEL` on a database or schema makes Snowflake automatically write records to the event table for every SP and UDF execution. No code change required in the SP itself.

```sql
-- Set at schema level (one event table per schema — see architecture section)
ALTER SCHEMA my_db.my_schema SET LOG_LEVEL   = 'WARN';
ALTER SCHEMA my_db.my_schema SET TRACE_LEVEL = 'ON_EVENT';
ALTER SCHEMA my_db.my_schema SET METRIC_LEVEL = 'NONE';     -- see below

-- Bind the event table to the schema
ALTER SCHEMA my_db.my_schema SET EVENT_TABLE = my_db.my_schema.EVENTS;
```

**Requires `GRANT MODIFY LOG LEVEL ON ACCOUNT TO ROLE DEPLOYER`** — object ownership alone is not enough.

---

## The three levels

### LOG_LEVEL — Python logging output

Captures `logging.getLogger()` output from SP/UDF code. Standard Python log levels.

| Value | Captures |
|-------|---------|
| `TRACE` | Everything |
| `DEBUG` | Debug + above |
| `INFO` | Info + above |
| `WARN` | Warnings + errors only (recommended default) |
| `ERROR` | Errors only |
| `FATAL` | Fatal only |
| `OFF` | Nothing |

**Recommended default: `WARN`** — avoids flooding the event table with INFO noise from routine runs. Raise to `DEBUG` temporarily when diagnosing a specific issue.

### TRACE_LEVEL — span execution telemetry

Captures SP/UDF execution as OpenTelemetry spans — start time, duration, status, and any attributes set via `set_span_attribute()`.

| Value | Captures |
|-------|---------|
| `ALWAYS` | Every execution, regardless of outcome |
| `ON_EVENT` | Only when the SP emits at least one event or attribute (recommended default) |
| `OFF` | Nothing |

**Recommended default: `ON_EVENT`** — if every SP call always sets at least one span attribute on exit, every execution still produces a span. `ALWAYS` would also capture SPs that emit nothing, which is not useful.

### METRIC_LEVEL — automatic counters and gauges

Captures automatic metrics: SP call count, execution duration. These are Snowflake-generated — you cannot add custom metrics via the Python API yet (`add_metric()` not yet exposed).

**Recommended value: `NONE`**

Duration and call count are already in the trace span (start/end time + whatever span attributes your SPs set). `METRIC_LEVEL=ALL` adds no information that isn't already in `TRACE_LEVEL=ON_EVENT`. When Snowflake exposes `add_metric()`, a telemetry wrapper can be updated to emit custom metrics without changing the SP's contract.

---

## Adding custom telemetry — `snowflake-telemetry`

Manual records written from SP/UDF code via the `snowflake.telemetry` package.

```python
from snowflake.telemetry import add_event, set_span_attribute

# Flat span attributes (renders as columns in Snowsight)
set_span_attribute("rows_processed", 1542)
set_span_attribute("run_date", "2026-01-15")
set_span_attribute("status", "ok")

# Span event — point-in-time annotation within a span
add_event("batch_complete", {"batch_id": 3, "rows": 512})
```

**Flat attributes vs JSON strings** — always use flat `set_span_attribute(key, value)` with native types (`int`, `str`, `float`). JSON strings require parsing in Snowsight and lose type information. Snowsight renders flat attributes as typed columns.

A thin wrapper that accumulates attributes during the SP and emits them once at exit (rather than
calling `set_span_attribute` scattered through the code) keeps this consistent across SPs — worth
building once per codebase rather than per SP.

### OpenTelemetry

Snowflake's telemetry API is built on OpenTelemetry. For teams that need more control (custom exporters, multi-backend tracing), the full `opentelemetry-sdk` works inside SPs. Overkill for a standard pipeline — `snowflake-telemetry` is sufficient.

---

## Storage lifecycle policy — mandatory

Without a lifecycle policy, the event table grows indefinitely. Old traces have no operational value and inflate storage billing.

```sql
-- 90-day retention (recommended baseline)
CREATE STORAGE LIFECYCLE POLICY my_db.my_schema.EVENTS_LIFECYCLE
  ON_EXPIRY_ACTION = DELETE
  AFTER DAYS = 90;

ALTER EVENT TABLE my_db.my_schema.EVENTS
  SET STORAGE LIFECYCLE POLICY my_db.my_schema.EVENTS_LIFECYCLE;
```

**Always attach a lifecycle policy at event table creation.** Do not add it retroactively — the table will have accumulated months of data by then.

Retention guidance:
- **90 days** — standard for debugging and incident lookback
- **30 days** — acceptable if storage cost is a concern and incidents are resolved quickly
- **> 90 days** — only for compliance requirements

---

## Architecture — one event table per schema

```
my_db
├── my_schema_a
│   ├── EVENTS              ← schema-local event table
│   └── EVENTS_LIFECYCLE    ← 90-day retention policy
└── my_schema_b
    ├── EVENTS
    └── EVENTS_LIFECYCLE
```

**Why per-schema, not per-database:**
- Snowsight Monitoring UI is scoped per event table — per-schema isolation makes the Snowsight view clean and relevant per team/pipeline
- `ALTER DATABASE SET EVENT_TABLE` points to a shared table — all schemas' traces mixed together, poor Snowsight UX
- `ALTER SCHEMA SET EVENT_TABLE` overrides the database default for that schema

Per-schema is the recommended default. The database-level event table can serve as a fallback for schemas that don't have their own.

---

## Gotchas

- **`MODIFY LOG LEVEL` grant is required** — ownership alone doesn't allow `ALTER SCHEMA SET LOG_LEVEL`. Without the account-level grant, CI/CD fails silently or with permission error.
- **`METRIC_LEVEL=ALL` is superfluous once tracing is in place** — count and duration values are already in span attributes if your SPs set them on exit. `add_metric()` doesn't exist in the Python API yet — when it does, a telemetry wrapper can expose it without changing SP code.
- **`TRACE_LEVEL=ALWAYS` floods the table** — use `ON_EVENT`. Any SP that sets at least one span attribute will emit a span anyway.
- **Log levels are cumulative downward** — `WARN` captures WARN + ERROR + FATAL. Not like Python logging where you set the minimum level.
- **Event table is append-only** — you cannot update or delete individual records. Only the lifecycle policy (bulk expiry) removes data.
- **Snowsight is the primary interface** — raw SQL on `EVENT_TABLE` is for automation/alerting only. Snowsight renders spans as flame graphs and attribute tables, which raw SQL cannot replicate.
