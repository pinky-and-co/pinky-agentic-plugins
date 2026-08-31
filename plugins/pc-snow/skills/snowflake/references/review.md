# Snowflake stack — review checklist

Applied on top of `pc-core:review-guidelines` when the code under review deploys to or runs on
Snowflake (Snowpark stored procedures / UDFs, `snowflake.core` IaC, serverless tasks, a `resources/`
pipeline layout). Severity labels are the same set. Background for each area is in the sibling
references (`core-api.md`, `telemetry.md`, `gov.md`, `warehouses.md`).

## Deployment (`snowflake.core`)

- **🔴 blocking** — an object created with `create_or_update` / `create(mode=...)` where the intent
  is idempotent redeploy but the mode drops and recreates data-bearing objects (Table, Stage with
  files). Check the mode against whether the object holds state.
- **🟡 important** — a hardcoded warehouse, database, or schema name where the deploy role's context
  or a naming convention should resolve it.
- **🟡 important** — `Root` built from a fresh connector inside code that already has a Snowpark
  session available (inside Snowflake, pass the active session).

## Stored procedures / UDFs

- **🔴 blocking** — a serverless task or procedure body that writes rows or emits events on every
  run with no change-detection guard (the universal "no-op guard" check, and on Snowflake each
  empty run still bills warehouse or serverless credits).
- **🟡 important** — `logging` calls at `INFO` carrying operational signal. The event table's
  `LOG_LEVEL` is `WARN` in production (see `telemetry.md`) — an abnormal-but-handled case logged at
  `INFO` is invisible. Use `warning`.
- **🟡 important** — a broad `EXECUTE AS OWNER` procedure that takes caller-supplied identifiers and
  interpolates them into a SQL string (privilege escalation via injection).

## Telemetry / event table (data repos with a `resources/` layout)

- **🟡 important** — new SP/UDF code that adds its own bespoke logging table or print-to-stdout
  instead of standard `logging` / `snowflake.telemetry` — the schema's event table already captures
  it, and Snowsight Monitoring is the consumption interface, not raw SQL.
- **🟡 important** — a schema-level `LOG_LEVEL` / `TRACE_LEVEL` / `EVENT_TABLE` change committed in a
  pipeline resource without the matching `GRANT MODIFY LOG LEVEL` noted — the deploy will fail on a
  role that only owns the objects.
- **🟢 nit** — `METRIC_LEVEL` left `ALL` on a schema with high-frequency tasks (event-table volume
  with no consumer).

## Governance (`gov.md`)

- **🟡 important** — a column holding PII (name, email, national id) created without a masking
  policy or classification tag, in a repo that declares governance elsewhere.
