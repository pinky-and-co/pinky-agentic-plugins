# Snowflake Warehouses, Cache & Compute Pools

Reference: Snowflake Service Consumption Table (effective 2026-07-31) + Snowpark Container Services.
Covers: warehouse types and sizing, the parameters that drive their cost, result cache, Streamlit
cache strategy, and compute pools for Container Runtime. $/credit pricing in `references/costs.md`.

---

## 1. Warehouse types — comparison

| Type | Min. billing | Billed per | Best suited for |
|---|---|---|---|
| Standard | 1 minute | Second (after 1 min) | General-purpose compute — the default choice absent a more specific need |
| Gen 2 | 1 minute | Second | Same workloads as Standard, on newer hardware/software — faster at a premium rate (see rates below); worth it when the performance gain justifies the cost delta |
| Adaptive | None (charged from first query) | Query-based — see below | Variable-size workloads (analytics, ETL, data loading) where you'd rather not hand-tune size/scaling/QAS/suspend — see dedicated section |
| Interactive | **60 minutes** | Second (after 60 min) | Long-running interactive sessions where the 60-minute minimum is amortized — a poor fit for short or bursty sessions |
| Snowpark Optimized | 1 minute | Second | Snowpark workloads needing a large in-memory DataFrame on a single node (high per-node RAM) |
| Serverless (Tasks) | None | Second of actual compute | Scheduled/triggered pipelines where Snowflake manages compute directly — no warehouse to size, suspend, or scale |

### Standard rates (credits/hour)

| XS | S | M | L | XL | 2XL | 3XL | 4XL | 5XL | 6XL |
|----|---|---|---|----|-----|-----|-----|-----|-----|
| 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512 |

5XL/6XL availability may be restricted (large-instance sizes) — verify via `context7` or your
account before planning around them.

### Gen 2 rates (credits/hour, by cloud provider)

| Cloud | XS | S | M | L | XL | 2XL | 3XL | 4XL |
|---|---|---|---|---|---|---|---|---|
| AWS | 1.35 | 2.7 | 5.4 | 10.8 | 21.6 | 43.2 | 86.4 | 172.8 |
| Azure | 1.25 | 2.5 | 5 | 10 | 20 | 40 | 80 | 160 |
| GCP | 1.35 | 2.7 | 5.4 | 10.8 | 21.6 | 43.2 | 86.4 | 172.8 |

### Snowpark Optimized rates (credits/hour, by resource constraint)

| Resource constraint | XS | S | M | L | XL | 2XL | 3XL | 4XL | 5XL | 6XL |
|---|---|---|---|---|---|---|---|---|---|---|
| MEMORY_1X | 1.00 | 2.00 | 4.00 | 8.00 | 16.00 | 32.00 | 64.00 | 128.00 | – | – |
| MEMORY_1X_x86 | 1.10 | 2.20 | 4.40 | 8.80 | 17.60 | 35.20 | 70.40 | 140.80 | – | – |
| MEMORY_16X | – | – | 6.00 | 12.00 | 24.00 | 48.00 | 96.00 | 192.00 | 384.00 | 768.00 |
| MEMORY_16X_x86 | – | – | 6.25 | 12.50 | 25.00 | 50.00 | 100.00 | 200.00 | – | – |
| MEMORY_64X | – | – | – | 15.00 | 30.00 | 60.00 | 120.00 | 240.00 | – | – |
| MEMORY_64X_x86 | – | – | – | 16.00 | 32.00 | 64.00 | 128.00 | 256.00 | – | – |

### Interactive rates (credits/hour)

| XS | S | M | L | XL | 2XL | 3XL | 4XL |
|---|---|---|---|---|---|---|---|
| 0.6 | 1.2 | 2.4 | 4.8 | 9.6 | 19.2 | 38.4 | 76.8 |

### Adaptive — no per-size rate table

Adaptive Compute is billed **per query**, not per warehouse-size-hour — there is no credits/hour
table to look up. See the dedicated Adaptive Compute section below for the billing model.

### Serverless Tasks rates (multiplier on 1 credit/compute-hour)

| Feature | Compute Multiplier | Cloud Services Multiplier |
|---|---|---|
| Serverless Tasks | 0.9 | 1 |
| Serverless Tasks Flex | 0.5 | 1 |

A compute-hour = XS warehouse for 1 hour. Serverless Tasks cost **0.9× an XS** per hour of actual
compute, with no 60-second minimum per startup.

### Cloud Services — automatic waiver

Cloud services (metadata, optimization): billed at 4.4 credits/hour.
**Automatically waived if ≤ 10% of daily Virtual Warehouse Credits.**
No action required — Snowflake applies the adjustment automatically.

---

## 2. Cost-impacting parameters — AUTO_RESUME, AUTO_SUSPEND, SCALING_POLICY

Three warehouse properties directly control cost beyond size — set these deliberately rather than
leaving defaults in place.

### AUTO_SUSPEND

Seconds of inactivity before the warehouse suspends. Billing stops immediately on suspend — this
is the single biggest lever against idle cost.

Tradeoff: a shorter `AUTO_SUSPEND` cuts idle billing but increases how often the warehouse resumes,
and every resume re-triggers the **1-minute minimum billing** (see rates above). For a warehouse
queried every few seconds, a short suspend just adds resume-minimum overhead for no benefit. For a
warehouse queried in bursts with long gaps between, a short suspend (e.g. 60s) captures real savings.

```sql
ALTER WAREHOUSE my_wh SET AUTO_SUSPEND = 60;   -- aggressive: bursty, infrequent load
ALTER WAREHOUSE my_wh SET AUTO_SUSPEND = 300;  -- looser: frequent load, resume overhead matters more
```

### AUTO_RESUME

Whether the warehouse auto-starts on the next query (`TRUE`, the near-universal default) or stays
suspended until manually resumed (`FALSE`). `FALSE` is a rare choice — it trades convenience for
explicit control over exactly when compute (and cost) starts, e.g. a warehouse that should only
ever run at a scheduled time and never on an ad hoc query.

```sql
ALTER WAREHOUSE my_wh SET AUTO_RESUME = TRUE;
```

### SCALING_POLICY (multi-cluster warehouses only)

Controls how aggressively a multi-cluster warehouse adds clusters under queuing load. Only
relevant once `MAX_CLUSTER_COUNT > 1`.

| Policy | Behavior | Suited to |
|---|---|---|
| **Standard** | New cluster after ~20s of queuing | Sustained, long-running load |
| **Economy** | Waits ~6 min of sustained load before scaling | Short, transient concurrency spikes |

**Billing: credits = rate_per_size × number_of_active_clusters.** A new cluster is billed at the
**1-minute minimum**, same as starting a single-cluster warehouse — `Standard` will spin up (and
bill) a cluster for a spike lasting only a few seconds, which over-provisions if spikes are
genuinely short-lived. `Economy` filters out transient spikes before scaling, at the cost of more
queuing during the wait window.

```sql
CREATE WAREHOUSE IF NOT EXISTS my_wh
  WAREHOUSE_SIZE       = 'XSMALL'
  AUTO_SUSPEND         = 120
  AUTO_RESUME          = TRUE
  MIN_CLUSTER_COUNT    = 1
  MAX_CLUSTER_COUNT    = 3
  SCALING_POLICY       = 'ECONOMY';
```

**Observing multi-cluster behavior:**

```sql
SELECT
    DATE_TRUNC('hour', start_time)  AS hour,
    MAX(avg_running)                AS peak_concurrent_queries,
    MAX(avg_queued_load)            AS peak_queued_queries,
    MAX(avg_cluster_count)          AS max_clusters_used
FROM snowflake.account_usage.warehouse_load_history
WHERE warehouse_name = 'MY_WH'
  AND start_time >= DATEADD(day, -30, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY peak_concurrent_queries DESC
LIMIT 20;
```

If `peak_queued_queries` stays high after several weeks at the current `MAX_CLUSTER_COUNT` →
raise it. If `max_clusters_used` never exceeds 1 → multi-cluster is buying nothing, revert to a
single cluster.

### The serverless alternative — skip these three parameters entirely (Tasks only)

Tasks can run without a warehouse (serverless mode) — Snowflake manages compute directly, billed
per second of actual use with no idle cost and no `AUTO_SUSPEND` / `AUTO_RESUME` / `SCALING_POLICY`
to tune.

**Dynamic Tables have no serverless mode — a warehouse is always required.** Both the initial full
refresh and every incremental refresh run on the warehouse declared in the DDL (`WAREHOUSE = ...`)
or the Python `DynamicTable` model (`warehouse=`). The older way to handle this was a dual-warehouse
setup — a large warehouse sized for the one-time full load, a small one for ongoing incremental
refreshes — or multi-clustering to cover both patterns on one warehouse. An **Adaptive Warehouse**
is the current best fit instead: it absorbs both the heavy one-time initialization and the smaller
incremental refreshes on its own, without a dual-warehouse setup or manual size tuning — see the
Adaptive Compute section below.

---

## 3. Adaptive Compute — new warehouse type (GA, Enterprise Edition+)

A workload-aware compute engine that replaces fixed warehouse sizing. Snowflake decides how to
allocate resources per query instead of you picking a t-shirt size. With an Adaptive Warehouse you
stop managing: warehouse size, multi-cluster settings, Query Acceleration Service config, and
suspend/resume policies.

All Adaptive Warehouses in an account share **one compute pool, dedicated to that account** — not
shared cross-account, not shared with standard/interactive/Snowpark-optimized warehouses. You can
still create several Adaptive Warehouses for workload grouping, chargeback, or governance.

### When to consider it

- Analytical workloads, data loading pipelines, mixed BI/ETL, or workloads with high size variance
  (queries ranging XSMALL to XLARGE) — Snowflake's own guidance for where it outperforms fixed sizing.
- **Dynamic Table refreshes** — handles both the heavy one-time/full-refresh load and the smaller
  incremental refreshes without a dual-warehouse setup or manual size tuning (see the note under
  Cost-impacting parameters above).
- Wanting less operational tuning: no warehouse size, multi-cluster, QAS, or suspend/resume to manage.
- A workload with bursty, session-driven concurrency or highly variable per-query size is a good
  pilot candidate before assuming a fixed size + multi-cluster Economy policy is optimal — that's
  exactly the profile Adaptive targets.

### When not to

- Warehouses used primarily for HTAP (hybrid transactional/analytical) — stay on standard Gen2.
- You need direct manual control over size and scaling policy for predictable workloads.
- Very low latency, user-facing queries (dashboards, apps) — use Interactive instead.
- High-memory Snowpark/ML on a single node — use Snowpark Optimized instead.

### Limitations

- Requires **Enterprise Edition or higher**.
- **Region-restricted** — available in select AWS/Azure/GCP regions only; verify current coverage
  via `context7` or your account before planning a migration.
- Cannot convert to/from an X5Large or X6Large warehouse.
- Cannot convert to/from a Snowpark-optimized or Interactive warehouse.

### The two tunable properties

| Property | Type | Default | Controls |
|---|---|---|---|
| `MAX_QUERY_PERFORMANCE_LEVEL` | t-shirt size, `XSMALL`–`X4LARGE` | `XLARGE` | Upper bound on performance Snowflake will apply to a single query when it's highly confident doing so helps. Doesn't map to a fixed compute config — it's a ceiling, not a size. |
| `QUERY_THROUGHPUT_MULTIPLIER` | non-negative integer | `2` | Scale factor for how many queries / how much total work can run at once, relative to a system-computed baseline. `0` = unlimited burst. Higher → less queuing, more spend risk. Lower → tighter spend, more queuing. |

### Create, convert, enable/disable

```sql
-- Create with defaults (MAX_QUERY_PERFORMANCE_LEVEL = XLARGE, QUERY_THROUGHPUT_MULTIPLIER = 2)
CREATE ADAPTIVE WAREHOUSE my_adaptive_wh;

-- Create with explicit properties
CREATE ADAPTIVE WAREHOUSE my_adaptive_wh
  WITH MAX_QUERY_PERFORMANCE_LEVEL = LARGE
       QUERY_THROUGHPUT_MULTIPLIER = 4;

-- Convert an existing standard warehouse — online, no downtime
ALTER WAREHOUSE my_warehouse SET WAREHOUSE_TYPE = 'ADAPTIVE';
-- Snowflake derives MAX_QUERY_PERFORMANCE_LEVEL / QUERY_THROUGHPUT_MULTIPLIER automatically
-- from the warehouse's prior size, MAX_CLUSTER_COUNT, QAS setting, and generation.

-- Convert back to standard
ALTER WAREHOUSE my_warehouse SET WAREHOUSE_TYPE = 'STANDARD';

-- Enable/disable — reject new jobs without dropping the warehouse
ALTER WAREHOUSE my_warehouse DISABLE;
ALTER WAREHOUSE my_warehouse ENABLE;
```

Bulk-convert many standard warehouses at once with `SYSTEM$BULK_UPDATE_WH` (name-pattern or
tag-filterable, dry-run first):

```sql
-- Dry run
SELECT SYSTEM$BULK_UPDATE_WH(
  'WAREHOUSE_TYPE', 'ADAPTIVE', '{"WAREHOUSE_TYPE": "STANDARD"}', 'DRY_RUN'
);
-- Then, after reviewing the output:
SELECT SYSTEM$BULK_UPDATE_WH(
  'WAREHOUSE_TYPE', 'ADAPTIVE', '{"WAREHOUSE_TYPE": "STANDARD"}', 'ACTIVE'
);
```

### Billing model — per query, not per warehouse-size-hour

Cost depends on the compute and software resources each query actually used (QAS usage is folded
into compute credits — no separate QAS line for Adaptive). No charge for creating the warehouse;
billing starts on the first query. Reported as `COMPUTE` in usage statements using standard virtual
warehouse credits — Budgets and Resource Monitors both still apply.

### Monitoring

```sql
-- Per-query credit usage (up to 1h lag)
SELECT * FROM snowflake.account_usage.query_metering_history
WHERE warehouse_name = 'MY_ADAPTIVE_WH'
ORDER BY start_time DESC;

-- Compare Adaptive vs standard warehouses on timing/queuing over the last 7 days
WITH adaptive_whs AS (
  SELECT DISTINCT warehouse_name
  FROM snowflake.account_usage.query_history q
  WHERE q.warehouse_size = 'ADAPTIVE'
    AND q.start_time >= DATEADD(day, -7, CURRENT_DATE())
)
SELECT
  q.end_time::DATE                    AS ds,
  q.warehouse_name,
  AVG(q.total_elapsed_time)           AS avg_query_time,
  AVG(q.queued_overload_time)         AS avg_queued_overload_time
FROM snowflake.account_usage.query_history q
WHERE q.start_time >= DATEADD(day, -7, CURRENT_DATE())
  AND q.warehouse_name IN (SELECT warehouse_name FROM adaptive_whs)
GROUP BY ALL;
```

Use `WAREHOUSE_LOAD_HISTORY` to check for queuing and decide whether to raise
`QUERY_THROUGHPUT_MULTIPLIER` (more burst capacity) — the same view used for multi-cluster
`SCALING_POLICY` tuning above.

---

## 4. Cache — 4 levels, strategy per data type

### Overview

| Level | Scope | TTL | Managed by |
|---|---|---|---|
| Snowflake result cache | Whole account | 24h (reset if data changes) | Automatic |
| `st.cache_resource` | Per app, all users | No TTL (until restart) | Developer |
| `st.cache_data` | Per app, all users (1.53+: per session) | Configurable | Developer |
| `st.session_state` | Per user, per session | End of session | Developer |

> ⚠️ Streamlit cache is scoped **per app**, not per compute pool node.
> Two apps on the same compute pool each have their own memory space — no sharing between
> one app and another.

### Strategy by data type

#### Heavy join view (per-session data) → `st.session_state`

- Data modified during the session → cannot be shared between users
- 1st user of the session → hits warehouse → result stored in `st.session_state`
- Snowflake result cache absorbs repetitions (monthly data → cache almost always valid)
- Pattern:

```python
if "joined_df" not in st.session_state:
    conn = st.connection("snowflake")
    st.session_state["joined_df"] = conn.query("""
        SELECT c.*, h.ref_id, o.category_label
        FROM {schema}.local_table c
        JOIN DATA_HUB__EXAMPLE.reference_hub h ON c.id = h.local_id
        JOIN DATASOURCE__OPEN_DATA.reference_data o ON h.ref_id = o.ref_id
    """)
```

#### Near-static reference data (autocomplete) → `st.cache_resource`

- Near-static data (refreshed 1x/month)
- Loaded once at container startup, shared across all users/clients
- `st.cache_resource` preferred over `st.cache_data`: no re-serialization on each access, no TTL
  to manage
- Refresh: container restart triggered by the monthly reference-data refresh Task

```python
@st.cache_resource
def load_reference_data() -> pd.DataFrame:
    conn = st.connection("snowflake")
    return conn.query("""
        SELECT ref_id, label, address, category_code, category_label
        FROM DATASOURCE__OPEN_DATA.REFERENCE.ENTRIES
        WHERE status = 'A'
    """).to_pandas()

df_reference = load_reference_data()
```

#### Shared data with TTL (dashboards, KPIs) → `st.cache_data`

Since 1.53.0: `st.cache_data` can be scoped per session (`experimental_allow_widgets=True`).
For data shared between users with a defined freshness:

```python
@st.cache_data(ttl=3600)   # 1 hour
def load_dashboard_kpis(tenant_id: str) -> pd.DataFrame:
    conn = st.connection("snowflake")
    return conn.query(f"SELECT * FROM {tenant_id}.kpis WHERE run_date = CURRENT_DATE")
```

### Reference data memory estimate

```sql
-- Active-column volume for a reference dataset (before loading into memory)
SELECT
    COUNT(*)                        AS nb_rows,
    AVG(LENGTH(label))              AS avg_len_label,
    AVG(LENGTH(address))            AS avg_len_address,
    AVG(LENGTH(category_code))      AS avg_len_category
FROM DATASOURCE__OPEN_DATA.REFERENCE.ENTRIES
WHERE status = 'A';
```

```python
# Precise measurement after loading
df = session.table("...").to_pandas()
print(f"{df.memory_usage(deep=True).sum() / 1024**2:.0f} MB")
```

Calculation rule:
- String → `average_length × nb_rows × 1.5` (Python overhead)
- Date / int → `8 bytes × nb_rows`
- Total → sum + 20% DataFrame overhead

Size the target node's RAM against this estimate before assuming several apps can run comfortably
in parallel on the same node.

---

## 5. Compute Pool

Used by SiS Container Runtime apps.

### Default configuration (system compute pool)

| Property | Value | Note |
|---|---|---|
| Instance family | `CPU_X64_S` | — |
| MIN_NODES | 1 | — |
| MAX_NODES | 150 | Adjust based on nb_apps × concurrent_users |
| AUTO_SUSPEND_SECS | 259 200 (3 days) | **Too long — reduce** (see below) |
| Initially suspended | ✅ Yes | No cost until the first workload |

### `CPU_X64_S` node specs (Gen 1 SPCS)

| vCPU | RAM | Cost (credits/hour) |
|---|---|---|
| 3 | 13 GB | **0.11** |

> Snowflake maintains **1 free idle node** permanently while the pool is active —
> this node does not appear in billing. When a workload starts on it,
> it becomes normally billable and Snowflake provisions a new free idle node as a replacement.

### Placement constraint — one container = one node

All containers of a service instance run on **a single node** — an instance cannot
span multiple nodes. If 2 apps saturate a node's RAM, Snowflake schedules the new
instances on a different node (automatic autoscaling up to MAX_NODES).

### Lifecycle management

```sql
-- Manually suspend after prolonged inactivity
ALTER COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU SUSPEND;

-- Reduce auto-suspend (default 3 days → too long to save cost)
ALTER COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU SET AUTO_SUSPEND_SECS = 3600;

-- Use a custom pool for Streamlit apps
ALTER ACCOUNT SET DEFAULT_STREAMLIT_COMPUTE_POOL = 'MY_CUSTOM_POOL';

-- Check current config
SHOW PARAMETERS LIKE 'DEFAULT_STREAMLIT_COMPUTE_POOL' IN ACCOUNT;
```

### Pool states

| State | Description |
|---|---|
| `IDLE` | Nodes available, no active service — autoscaling can reduce to MIN |
| `ACTIVE` | At least 1 service running — pool can grow up to MAX |
| `SUSPENDED` | No active nodes — resumes automatically if `AUTO_RESUME = TRUE` |
| `STARTING` | Provisioning in progress (transient) |
| `RESIZING` | Adding/removing nodes (transient) |

### Snowflake rolling maintenance

Snowflake replaces nodes regularly (every few weeks):

- **Usual windows:** Sat 8pm → Sun 8am, Sun 8pm → Mon 8am
- With a single instance → possible disruption during maintenance
- With multiple instances → Snowflake replaces max 50% simultaneously → capacity always available

**Do not schedule critical Tasks during these windows.**

### Restrict pool access (default: PUBLIC)

By default, `USAGE` is granted to role `PUBLIC` → all roles in the account can use the pool.

```sql
USE ROLE ACCOUNTADMIN;
REVOKE USAGE ON COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU FROM ROLE PUBLIC;
GRANT USAGE ON COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU TO ROLE MY_APP_ROLE;
```

### Associate the pool with a budget

```sql
CALL my_budget!ADD_RESOURCE(
  SYSTEM$REFERENCE('COMPUTE_POOL', 'SYSTEM_COMPUTE_POOL_CPU', 'SESSION', 'applyBudget')
);
```

See `references/costs.md` for the full Budget/Quota mechanics.

### Account limit

Maximum **750 nodes** total (across all compute pools), with limits per instance family.
Monitor if the number of SPCS apps grows.

---

## 6. Consumption metrics

### Warehouse — credits per day

```sql
SELECT
    warehouse_name,
    DATE(start_time)         AS day,
    SUM(credits_used)        AS credits
FROM snowflake.account_usage.warehouse_metering_history
WHERE start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2
ORDER BY 1, 2;
```

### Compute pool — credits per day

```sql
SELECT
    compute_pool_name,
    DATE(start_time)         AS day,
    SUM(credits_used)        AS credits
FROM snowflake.account_usage.metering_history
WHERE service_type = 'SNOWPARK_CONTAINER_SERVICES'
  AND start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY 1, 2
ORDER BY 2;
```

### Serverless Tasks — credits per Task

```sql
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

---

## Gotchas

- **Interactive Warehouse: 60 min minimum billing** — never use for short, bursty sessions.
  A 2-minute session on an Interactive Warehouse bills 60 minutes.
- **Multi-cluster: credits = rate × number_of_active_clusters** — an XSMALL cluster with 2 active
  clusters = 2 credits/hour. Validate that the Economy policy does prevent unnecessary spinning via
  `warehouse_load_history`.
- **Compute pool: no automatic sleep** — unlike warehouses, an active compute pool bills
  continuously. `ALTER COMPUTE POOL ... SUSPEND` is manual, or reduce `AUTO_SUSPEND_SECS`.
- **`st.cache_resource` does not clear on code change** — only a container restart purges the
  cache. A schema change in cached reference data requires an explicit restart.
- **Result cache invalidated if data changes** — even a single row modified in a source table
  invalidates the cache for all queries reading that table. For tables refreshed monthly, the
  cache holds for 30 days.
- **Account Usage: lag up to 3h** — `warehouse_metering_history` and `metering_history` are not
  real-time. Use `INFORMATION_SCHEMA` for near-real-time monitoring.
- **750 nodes max per account** — global limit across all compute pools combined. Not an issue at
  the start but to document if SPCS grows.
- **Gen 2 SPCS vs Gen 1** — `SYSTEM_COMPUTE_POOL_CPU` uses `CPU_X64_S` (Gen 1, 0.11 credits/hour).
  Gen 2 instances (`GEN_X64_G2_*`) have different rates and are not available via the system pool.
  Check availability before creating a custom Gen 2 pool.
- **Adaptive Warehouses can't take standard sizing properties** — `WAREHOUSE_SIZE`,
  `MIN_CLUSTER_COUNT`, `MAX_CLUSTER_COUNT`, `SCALING_POLICY` don't apply once converted; conversely
  `MAX_QUERY_PERFORMANCE_LEVEL`/`QUERY_THROUGHPUT_MULTIPLIER` don't apply to a standard warehouse.
  `ALTER WAREHOUSE` fails if you mix the two property sets.
- **Adaptive conversion briefly double-bills** — it's online (no downtime), but already-running
  queries finish on the old compute while new queries start on the new type; both are billed during
  the overlap. Converting back to standard does **not** auto-suspend the warehouse during that window.
- **Adaptive Compute is Enterprise Edition+ and region-restricted** — don't assume it's available
  on a given account/region without checking; the conversion paths to/from X5Large, X6Large,
  Snowpark-optimized, and Interactive are also unsupported.
