# Snowflake cost model & tracking

Goal: for every activated feature, answer "concretely, what does it cost me?" with the right Account Usage query.

> Prices ($/credit) are region- and contract-dependent — not documented here. Only the **credit consumption model** is.
> Cortex LLM inference costs are out of scope here — see the `cortex` skill's `cortex-ai-functions.md` § Cost.
> For a consumption-model detail you're unsure of, query `context7` first (`libraryId: /websites/snowflake_en`)
> rather than trusting training data.

---

## 1. Consumption model — the fundamentals

Two billing modes. Everything else follows from this distinction.

### Virtual warehouse (classic compute)
- Billed **per second**, minimum 60 seconds per start
- Rate: credits/hour by size — XS=1, S=2, M=4, L=8, XL=16, 2XL=32, 4XL=64, 6XL=128
- Auto-suspend stops billing immediately when idle
- **Resumed by any query** — auto-suspend only saves money if the warehouse actually sits idle between queries

### Serverless compute
- Billed **per second of actual compute used** — no minimum, no idle cost
- Managed by Snowflake (no warehouse to configure or suspend)
- Used by: Tasks (serverless mode), Snowpipe, some Cortex functions
- **Rule**: Tasks are always serverless — never configure a virtual warehouse for Tasks

---

## 2. Cost per stack component

### Snowpark Stored Procedures

**Billing model**: warehouse compute — the SP runs on whichever warehouse the caller is using.

**Measurement**:
```sql
-- Credits consumed by warehouse, per day
SELECT
    warehouse_name,
    DATE(start_time)             AS day,
    SUM(credits_used)            AS credits
FROM snowflake.account_usage.warehouse_metering_history
WHERE start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2
ORDER BY 1, 2;
```

**Note**: SPs execute on the pipeline warehouse (declared in the Task or called directly). A dedicated deploy warehouse (XSMALL) should be used only for deployment — never for production pipeline execution. Keep its auto-suspend at 60s.

---

### Serverless Tasks

**Billing model**: serverless credits — billed per second of actual compute, no warehouse.

**Measurement**:
```sql
-- Credits consumed by serverless tasks, per task, per day
SELECT
    database_name,
    schema_name,
    name                         AS task_name,
    DATE(completed_time)         AS day,
    SUM(credits_used_cloud_services + credits_used_compute) AS credits
FROM snowflake.account_usage.serverless_task_history
WHERE completed_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2, 3, 4
ORDER BY credits DESC;
```

**Note**: Tasks running serverless by default (no `WAREHOUSE =` clause) is the recommended pattern. An over-triggered Task DAG (schedule too tight, upstream failures causing retries) is the main cost risk — monitor error-integration alerts and task history.

---

### Dynamic Tables

**Billing model**: warehouse compute for each refresh (initial full refresh + incremental
refreshes) — Dynamic Tables have **no serverless mode**; a warehouse is always required, declared
in the DDL or the Python `DynamicTable` model. An Adaptive Warehouse (see `references/warehouses.md`)
is the current recommended type: it absorbs both the heavy one-time full refresh and the smaller
incremental refreshes without a dual-warehouse setup or manual size tuning.

**Measurement**:
```sql
-- Credits consumed by dynamic table refreshes, per table
SELECT
    database_name,
    schema_name,
    name                         AS table_name,
    refresh_trigger,             -- SCHEDULED, UPSTREAM, MANUAL
    SUM(credits_used)            AS credits,
    COUNT(*)                     AS refresh_count,
    AVG(credits_used)            AS avg_credits_per_refresh
FROM snowflake.account_usage.dynamic_table_refresh_history
WHERE refresh_start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
  AND state = 'SUCCEEDED'
GROUP BY 1, 2, 3, 4
ORDER BY credits DESC;
```

**Note**: incremental refresh cost is proportional to changed rows, not table size. A DT with a long `LAG` (less frequent refresh) is cheaper — tune lag to the actual freshness requirement, not "as fast as possible". The `STATISTICS` column in refresh history (available since 2026-03-13) includes time distribution — use it to diagnose expensive refreshes.

---

### Streamlit in Snowflake — standard runtime

**Billing model**: warehouse compute — the app runs on the warehouse assigned to the SiS app.

**Measurement**:
```sql
-- Streamlit app warehouse consumption
SELECT
    warehouse_name,
    DATE(start_time)             AS day,
    SUM(credits_used)            AS credits
FROM snowflake.account_usage.warehouse_metering_history
WHERE warehouse_name = 'YOUR_SIS_WAREHOUSE'
  AND start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2
ORDER BY 2;
```

**Note**: standard runtime apps sleep after inactivity — the warehouse auto-suspends. Cost spikes happen at cold start (minimum 60s billing on first query after sleep). Design for cold starts; don't keep a dedicated warehouse running for a low-traffic app.

---

### Streamlit in Snowflake — container runtime

**Billing model**: Snowpark Container Services compute pool — billed per node-hour, regardless of app activity (no sleep).

**Measurement**:
```sql
-- Compute pool usage (container runtime)
SELECT
    compute_pool_name,
    DATE(start_time)             AS day,
    SUM(credits_used)            AS credits
FROM snowflake.account_usage.metering_history
WHERE service_type = 'SNOWPARK_CONTAINER_SERVICES'
  AND start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2
ORDER BY 2;
```

**Note**: container runtime has **no sleep** — the compute pool runs continuously. Suspend the compute pool when the app is not in use (`ALTER COMPUTE POOL ... SUSPEND`). Only use container runtime when you actually need GPU, unrestricted packages, or viewer URLs — not as a default.

---

### Cortex AI functions (in Dynamic Table incremental refresh)

**Billing model**: serverless credits per Cortex function call — charged at incremental row level inside DT refresh, not per full table scan.

**Measurement**:
```sql
-- Cortex function consumption
SELECT
    DATE(start_time)             AS day,
    service_type,
    SUM(credits_used)            AS credits
FROM snowflake.account_usage.metering_history
WHERE service_type = 'AI_SERVICES'
  AND start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2
ORDER BY 2;
```

**Note**: incremental Cortex in DTs (available since 2025-09-11) is cost-efficient because only changed rows trigger function calls. The risk is a DT with a large initial full refresh — first sync processes the entire table. Size the initial data volume before enabling.

---

### Openflow connectors

**Billing model**: BYOC (Bring Your Own Compute) — cost attributed to the compute running the connector, tracked in Account Usage since May 2026.

**Measurement**:
```sql
-- Openflow BYOC cost attribution
SELECT
    DATE(start_time)             AS day,
    service_type,
    SUM(credits_used)            AS credits
FROM snowflake.account_usage.metering_history
WHERE service_type = 'OPENFLOW'
  AND start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2
ORDER BY 2;
```

**Note**: some connector license models include a free trial that auto-commits to a long-term agreement afterward (observed: 60-day trial → 36-month auto-commit on an Oracle connector's Embedded License model, 2026-02-27). Always check whether an existing license (e.g. an XStream license via BYOL) avoids the auto-commit path.

---

## 3. Storage & retention

### Storage types and their cost

| Type | Billed | Controllable |
|---|---|---|
| Active table data | Per TB/month | Yes — delete rows, use compression |
| Time travel data | Per TB/month | Yes — `DATA_RETENTION_TIME_IN_DAYS` |
| Fail-safe data | Per TB/month | **No** — 7 days fixed for permanent tables |
| Stage files | Per TB/month | Yes — purge after processing |
| Event table data | Per TB/month | Yes — Storage Lifecycle Policy |

**Transient tables**: no fail-safe (0 days), time travel 0–1 days only. Use for staging tables and intermediate results — they cost significantly less storage than permanent tables.

```sql
CREATE TRANSIENT TABLE my_schema.stg_raw_load (...);
```

**Measurement**:
```sql
-- Storage breakdown by database
SELECT
    database_name,
    ROUND(average_database_bytes / POWER(1024, 3), 2)    AS active_gb,
    ROUND(average_failsafe_bytes / POWER(1024, 3), 2)    AS failsafe_gb,
    ROUND(average_stage_bytes / POWER(1024, 3), 2)       AS stage_gb
FROM snowflake.account_usage.database_storage_usage_history
WHERE usage_date = CURRENT_DATE - 1
ORDER BY active_gb DESC;
```

---

### Lifecycle policies — cost AND compliance

Storage lifecycle policies serve two independent drivers:

| Driver | Concern | Example |
|---|---|---|
| **Cost** | Don't pay for data with no operational value | Event table traces older than 90 days |
| **GDPR / compliance** | Legal obligation to purge or retain | PII deleted on RTBF request; financial records kept 5+ years |

**The mechanism is the same — the retention period differs.**

#### Event tables
See `references/telemetry.md` for full setup. Recommended baseline: 90 days.

```sql
CREATE STORAGE LIFECYCLE POLICY my_schema.EVENTS_LIFECYCLE
  ON_EXPIRY_ACTION = DELETE
  AFTER DAYS = 90;

ALTER EVENT TABLE my_schema.EVENTS
  SET STORAGE LIFECYCLE POLICY my_schema.EVENTS_LIFECYCLE;
```

#### Stage files
Snowflake does not currently expose a native `STORAGE LIFECYCLE POLICY` for stage files. Options:
- **Internal stages**: purge files after processing — `REMOVE @my_stage/path/file.csv` in the SP that loaded them. Do not leave processed files on stage.
- **External stages (S3/GCS/Azure)**: apply a bucket-level lifecycle policy on the cloud provider side. This is outside Snowflake's control — document the policy in the stage definition or in `snowflake-provider` manifest.

```sql
-- Purge processed file from internal stage (in SP, after successful load)
REMOVE @my_schema.MY_STAGE/processed/file.csv;

-- Or purge all files older than X days (manual / scheduled Task)
LIST @my_schema.MY_STAGE;  -- inspect first, then REMOVE selectively
```

#### Time travel and GDPR right to be forgotten (RTBF)

A `DELETE` or `UPDATE` on a permanent table does **not** immediately erase data — it remains accessible via time travel (up to `DATA_RETENTION_TIME_IN_DAYS`) and then in fail-safe (7 days, Snowflake support access only).

**Implications for GDPR RTBF**:
- Fail-safe cannot be disabled for permanent tables
- A GDPR deletion request is not fully honored until fail-safe expires (up to 7 days after time travel ends)
- For tables containing PII: set `DATA_RETENTION_TIME_IN_DAYS = 0` to minimize the window
- Use transient tables for staging data with PII — fail-safe = 0 days

```sql
-- Minimize time travel on PII table (fail-safe still applies for 7 days)
ALTER TABLE my_schema.pii_table SET DATA_RETENTION_TIME_IN_DAYS = 0;

-- Transient table: no fail-safe at all
CREATE TRANSIENT TABLE my_schema.stg_pii_load (...);
```

#### Legal retention periods — reference

| Data type | Minimum retention (FR/EU) | Recommended Snowflake pattern |
|---|---|---|
| Financial records | 10 years (Art. L. 123-22 French Commercial Code) | Permanent table, time travel 1 day, archive schema |
| HR / payroll | 5 years after departure | Permanent table, time travel 1 day |
| Contracts | Contract term + 5 years (limitation period) | Permanent table or stage, no auto-purge |
| PII (customers) | Duration of the relationship + legal grace period | Configurable — purge policy required |
| Logs / traces | No minimum obligation | Event table 90 days (cost-driven) |

> Retention periods are guidance, not legal advice. Validate with your DPO for the specific data category and jurisdiction.

---

## 4. Global cost visibility

### Resource Monitors — spend guardrails

Suspend or notify when credit consumption reaches a threshold. Applied at account or warehouse level.

```sql
-- Monitor on a specific warehouse
CREATE RESOURCE MONITOR pipeline_monitor
  WITH CREDIT_QUOTA = 100
  TRIGGERS
    ON 75 PERCENT DO NOTIFY
    ON 95 PERCENT DO SUSPEND;

ALTER WAREHOUSE my_pipeline_wh SET RESOURCE_MONITOR = pipeline_monitor;
```

**Limit**: Resource Monitors only cover warehouse-backed compute — they attach to a warehouse, so
they **do** cover Dynamic Tables (which always run on one). What they miss is genuinely serverless
compute: Tasks running serverless, Snowpipe, some Cortex functions. Use Budgets for those.

### Budgets (BUDGET_CREATOR role)

Snowflake-native cost governance — covers both warehouse and serverless. Requires `BUDGET_CREATOR` database role on the SNOWFLAKE database (provisioned in bootstrap).

```sql
-- Account-level budget (ACCOUNTADMIN or BUDGET_ADMIN)
CREATE SNOWFLAKE.CORE.BUDGET my_account_budget()
  WITH SCHEDULE MONTHLY
  CREDIT_QUOTA = 500;

-- Custom budget scoped to a specific schema
CREATE SNOWFLAKE.CORE.BUDGET pipeline_budget()
  WITH SCHEDULE MONTHLY
  CREDIT_QUOTA = 50;

-- Add objects to the custom budget
CALL pipeline_budget!ADD_RESOURCE(
  SYSTEM$REFERENCE('WAREHOUSE', 'MY_PIPELINE_WH', 'SESSION', 'applyBudget')
);
```

### Per-user quotas (QUOTA_CREATOR role)

Snowflake-native **per-user** spend caps (public preview 2026) — object `SNOWFLAKE.CORE.QUOTA`,
scoped to a database + schema. Where a Budget caps an **aggregate** and only *notifies/suspends*, a
quota caps consumption **independently per user** on a monthly (UTC-calendar) or daily cycle and can
**hard-block** a user at their limit. This is the mechanism for **per-tenant cost isolation**: in a
multi-tenant deployment where one user = one tenant, a quota stops any single tenant from consuming
past a flat allowance — the guardrail a Budget cannot give (a Budget would only see the aggregate and
one tenant could exhaust it for everyone).

Two axes distinguish it from Budgets and Resource Monitors:

| Mechanism | Granularity | Enforcement | Covers |
|---|---|---|---|
| Resource Monitor | account / warehouse | notify · suspend | warehouse compute only |
| Budget (`CORE.BUDGET`) | account / custom scope | notify (aggregate) | warehouse **+** serverless |
| Per-user quota (`CORE.QUOTA`) | **per user** (tag-scoped) | notify · **hard block** | AI **or** warehouse (one, not both) |

Constraints: a quota watches **either** AI **or** warehouse compute, never both. Scope is by
**Snowflake tag** (not a hardcoded user list) — a tag per tenant, combined with `INTERSECTION`/`UNION`.
Blocks lift automatically at the next cycle or when the limit is raised.

```sql
-- Privilege: ACCOUNTADMIN, or a custom role granted the SNOWFLAKE.QUOTA_CREATOR database role
-- plus CREATE SNOWFLAKE.CORE.QUOTA on the target schema.
CREATE SNOWFLAKE.CORE.QUOTA cost_mgmt_db.quotas.tenant_ai_quota();

-- Per-user monthly credit cap + hard block at 100%.
CALL tenant_ai_quota!SET_PER_USER_LIMIT(120);
CALL tenant_ai_quota!SET_BLOCK_ENFORCEMENT_ENABLED(TRUE);

-- AI-domain scope (a quota is AI-only OR warehouse-only).
CALL tenant_ai_quota!ADD_SHARED_RESOURCE('AI FUNCTION');

-- Scope to users carrying a tenant tag (tag-based, not a user list).
CALL tenant_ai_quota!SET_USER_TAGS(
  [[(SELECT SYSTEM$REFERENCE('TAG', 'cost_mgmt_db.tags.tenant', 'SESSION', 'APPLYBUDGET')), 'acme']],
  'INTERSECTION'
);

-- Inspect the live configuration (use for idempotent reconcile / drift detection).
CALL tenant_ai_quota!GET_CONFIG();
```

**Note**: no `snowflake.core` Python resource exists for `CORE.QUOTA` (verified 1.10.0;
preview-recent — reverify current coverage via `context7`) — it is driven purely by
`CREATE ... QUOTA name()` + `CALL name!METHOD(...)`. An IaC wrapper for this can only be
schema-scoped and SQL-DDL-mastered, without a typed SDK class to lean on. Reconcile via
`GET_CONFIG()` rather than a `SHOW`. The quota object itself is a governance mechanism —
no direct credit cost, like Budgets and Resource Monitors.

### Account Usage — daily summary

```sql
-- Daily credit consumption by service type (last 30 days)
SELECT
    DATE(usage_date)             AS day,
    service_type,
    SUM(credits_used_compute)    AS compute_credits,
    SUM(credits_used_cloud_services) AS cloud_credits
FROM snowflake.account_usage.metering_daily_history
WHERE usage_date >= DATEADD('day', -30, CURRENT_DATE)
GROUP BY 1, 2
ORDER BY 1 DESC, compute_credits DESC;
```

---

## 5. Optimization patterns

**Auto-suspend** — set aggressively on all warehouses. XSMALL warehouses: 60s. Larger warehouses: 120–300s (startup cost is higher, so slightly longer idle is acceptable).

**Transient tables for staging** — no fail-safe, minimal time travel. Use for any intermediate or raw load table that doesn't need recovery.

**`cache_result()` in Snowpark** — materializes a DataFrame to a temp table. Use when the same DataFrame is consumed more than once in a single SP — avoids re-executing the same query plan twice.

```python
df_enriched = df_raw.join(df_ref, on="ID").cache_result()
count = df_enriched.count()          # executes once (from temp table)
df_enriched.write.save_as_table(...) # executes once (from temp table)
```

**Dynamic Table lag** — set lag to the actual business freshness requirement. A `LAG = '1 hour'` DT refreshes 24× less than `LAG = '5 minutes'`. Don't default to the minimum.

**Processed file purge** — remove files from internal stages immediately after successful load. Don't accumulate unprocessed files — LIST operations on large stages are slow and files still bill storage.

**Time travel tuning** — default is 1 day. For large tables with frequent updates (high churn), time travel storage can exceed active storage. Reduce to 0 on staging/transient tables, keep at 1–7 days only for tables where recovery matters.

---

## 6. Gotchas

- **60-second minimum billing on warehouse start** — a query that takes 2 seconds still bills 60 seconds if it starts a suspended warehouse. This is the main reason to use serverless Tasks instead of warehouse Tasks for short, frequent jobs.
- **Fail-safe is not optional for permanent tables** — 7 days, fixed, Snowflake support access. It appears in storage billing even if you never use it. Transient tables eliminate this.
- **Resource Monitors only cover warehouse-backed compute** — they attach to a warehouse, so they catch runaway Dynamic Table refreshes (always warehouse-backed) but miss genuinely serverless compute: Tasks running serverless, Snowpipe, some Cortex functions. Use Budgets (SNOWFLAKE.CORE.BUDGET) for those.
- **Budgets notify, they don't cap per user** — a Budget alerts on an aggregate; it cannot stop one user (or tenant) from exhausting a shared allowance. For a hard per-user ceiling, use a per-user quota (SNOWFLAKE.CORE.QUOTA) with `SET_BLOCK_ENFORCEMENT_ENABLED(TRUE)` — AI-only or warehouse-only, tag-scoped.
- **Account Usage has a latency** — `ACCOUNT_USAGE` views lag up to 3 hours (some up to 45 minutes for recent additions). Don't use them for real-time spend alerts — use `INFORMATION_SCHEMA` views for near-real-time, `ACCOUNT_USAGE` for historical analysis.
- **Cloud services credits** — small overhead on every query (metadata, optimization). Usually < 10% of compute. Billed separately but visible in `metering_daily_history`.
- **Container runtime compute pools run continuously** — no auto-sleep. Suspend explicitly when not in use or cost accumulates 24/7.
- **GDPR DELETE ≠ immediate erasure** — time travel + fail-safe window means data persists 0–7 days after deletion. Document this window for your DPO.
