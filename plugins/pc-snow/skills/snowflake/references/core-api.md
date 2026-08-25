# Snowflake Python Core API

> For a class/method you're unsure of — including whether it exists yet, and how many object
> types the SDK currently covers — query `context7` first (`libraryId: /websites/snowflake_en`,
> covers the `snowflake.core.*` reference pages) rather than trusting training data or a version
> number written down here. The SDK grows with every release; a pinned version and an object
> count go stale the moment the next one ships.

---

## Root — entry point

```python
from snowflake.core import Root
from snowflake.snowpark import Session

# From a Snowpark Session (standard inside Snowflake / pinky-provider)
root = Root(session)

# From a connector (local / CI)
from snowflake.connector import connect
root = Root(connect(**CONNECTION_PARAMETERS))
```

`Root` accepts both `SnowflakeConnection` and `Session`. In pinky-provider, always pass the Snowpark session.

---

## Resource pattern

All Snowflake objects follow the same three-class pattern:

| Class | Role |
|---|---|
| `FooModel` (e.g. `Task`) | Pydantic model — declares the object's properties |
| `FooCollection` | Access via `root.[path].foos` — `create()`, `iter()` |
| `FooResource` | Access via `root.[path].foos["name"]` — `fetch()`, `create_or_alter()`, `drop()`, object-specific actions |

```python
# Create
task_ref = root.databases["mydb"].schemas["myschema"].tasks.create(
    Task(name="mytask", definition="CALL MY_SP()"),
    mode=CreateMode.or_replace
)

# Fetch existing
task_ref = root.databases["mydb"].schemas["myschema"].tasks["mytask"]
task_obj = task_ref.fetch()   # returns Task model with current state

# Alter
task_ref.create_or_alter(Task(name="mytask", definition="CALL MY_SP_V2()"))

# Drop
task_ref.drop(if_exists=True)
```

---

## CreateMode

```python
from snowflake.core import CreateMode

CreateMode.error_if_exists   # default — raises ConflictError if exists (≈ CREATE)
CreateMode.or_replace        # ≈ CREATE OR REPLACE
CreateMode.if_not_exists     # ≈ CREATE IF NOT EXISTS
```

pinky-provider convention: use `or_replace` for idempotent deploys, `if_not_exists` for bootstrap objects.

---

## Navigation hierarchy

The SDK's object coverage grows with each Snowflake release — check `context7` for the current
count and for any object type not shown below.

```python
root
│
│  # Account-level objects
├── .databases["db"]                           → DatabaseResource
├── .warehouses["name"]                        → WarehouseResource
├── .roles["name"]                             → RoleResource
├── .database_roles["name"]                    → DatabaseRoleResource
├── .users["name"]                             → UserResource
├── .grants                                    → GrantCollection
├── .network_policies["name"]                  → NetworkPolicyResource
├── .network_rules["name"]                     → NetworkRuleResource
├── .password_policies["name"]                 → PasswordPolicyResource
├── .notification_integrations["name"]         → NotificationIntegrationResource
├── .api_integrations["name"]                  → ApiIntegrationResource
├── .catalog_integrations["name"]              → CatalogIntegrationResource  # Iceberg
├── .external_volumes["name"]                  → ExternalVolumeResource      # Iceberg
├── .compute_pools["name"]                     → ComputePoolResource         # SPCS
├── .image_repositories["name"]               → ImageRepositoryResource     # SPCS
├── .secrets["name"]                           → SecretResource
├── .managed_accounts["name"]                 → ManagedAccountResource
│
│  # Database → Schema → Object
└── .databases["db"]
    └── .schemas["schema"]                     → SchemaResource
        │  # Pipelines & compute
        ├── .tasks["name"]                     → TaskResource
        ├── .dynamic_tables["name"]            → DynamicTableResource
        ├── .pipes["name"]                     → PipeResource
        ├── .streams["name"]                   → StreamResource
        ├── .sequences["name"]                 → SequenceResource
        │  # Storage
        ├── .tables["name"]                    → TableResource
        ├── .views["name"]                     → ViewResource
        ├── .iceberg_tables["name"]            → IcebergTableResource
        ├── .stages["name"]                    → StageResource
        ├── .event_tables["name"]              → EventTableResource
        │  # Code
        ├── .stored_procedures["name"]         → StoredProcedureResource
        ├── .functions["name"]                 → FunctionResource
        ├── .user_defined_functions["name"]    → UserDefinedFunctionResource
        │  # Apps & UI
        ├── .streamlits["name"]                → StreamlitResource           # SiS app lifecycle
        ├── .notebooks["name"]                 → NotebookResource
        ├── .services["name"]                  → ServiceResource             # SPCS
        ├── .image_repositories["name"]        → ImageRepositoryResource
        │  # Governance
        ├── .alerts["name"]                    → AlertResource
        ├── .tags["name"]                      → TagResource
        ├── .network_rules["name"]             → NetworkRuleResource
        ├── .secrets["name"]                   → SecretResource
        ├── .password_policies["name"]         → PasswordPolicyResource
        └── .artifact_repositories["name"]     → ArtifactRepositoryResource
```

Some newer or Snowsight-only surfaces (e.g. Openflow connectors/pipelines, at time of writing) have
no `CREATE` support in the SDK yet — verify current coverage via `context7` before assuming a gap is
permanent or that a listed resource is still accurate.

---

## Task

```python
from snowflake.core.task import Task
from snowflake.core.task import Cron

task = Task(
    name="MY_TASK",
    definition="CALL MY_SCHEMA.MY_SP(PARSE_JSON('{\"run_date\": \"2026-01-01\"}'))",

    # Serverless (no warehouse)
    user_task_managed_initial_warehouse_size="XSMALL",  # initial size hint only
    serverless_task_min_statement_size="XSMALL",
    serverless_task_max_statement_size="MEDIUM",

    # Schedule — only on root task
    schedule=Cron("0 6 * * *", "Europe/Paris"),  # daily at 06:00 Paris

    # DAG
    predecessors=["SCHEMA.PARENT_TASK"],          # list of fully qualified task names
    condition="SYSTEM$STREAM_HAS_DATA('MY_STREAM')",

    # Resilience
    suspend_task_after_num_failures=3,
    task_auto_retry_attempts=1,
    user_task_timeout_ms=3_600_000,               # 1 hour

    comment="Managed by snowflake-provider",
)

task_ref = root.databases["db"].schemas["schema"].tasks.create(task, mode=CreateMode.or_replace)
```

### TaskResource actions

```python
task_ref.execute()              # trigger immediately (ignores schedule)
task_ref.execute(retry_last=True)  # retry the last failed run
task_ref.resume()               # un-suspend
task_ref.suspend()              # suspend
task_ref.create_or_alter(task)  # idempotent update — full object expected
task_ref.drop(if_exists=True)
task_ref.fetch()                # returns Task model with current server state
```

**Important**: `create_or_alter()` expects the FULL task model. Any field not set will be set to NULL. Always pass all fields, not just changed ones.

---

## Dynamic Table

```python
from snowflake.core.dynamic_table import DynamicTable, DownstreamLag, TargetLag

dt = DynamicTable(
    name="MY_DT",
    target_lag=TargetLag(lag="1 minute"),         # or DownstreamLag() for downstream mode
    warehouse="MY_WH",                             # always required — no serverless option for Dynamic Tables (Adaptive is the recommended type, see warehouses.md)
    query="SELECT * FROM MY_SOURCE_TABLE",
    comment="Managed by snowflake-provider",
)

root.databases["db"].schemas["schema"].dynamic_tables.create(
    dt, mode=CreateMode.or_replace
)
```

### DynamicTableResource actions
```python
dt_ref.suspend()
dt_ref.resume()
dt_ref.refresh()       # trigger immediate refresh
dt_ref.fetch()         # returns DynamicTable model
dt_ref.drop()
```

---

## Table

```python
from snowflake.core.table import Table, TableColumn, PrimaryKey

table = Table(
    name="MY_TABLE",
    columns=[
        TableColumn(name="ID", datatype="NUMBER(38,0)", nullable=False),
        TableColumn(name="NAME", datatype="VARCHAR(256)"),
        TableColumn(name="CREATED_AT", datatype="TIMESTAMP_NTZ", default_value="CURRENT_TIMESTAMP()"),
    ],
    primary_key=PrimaryKey(column_names=["ID"]),
    comment="Managed by snowflake-provider",
)

root.databases["db"].schemas["schema"].tables.create(table, mode=CreateMode.if_not_exists)
```

---

## Alert

```python
from snowflake.core.alert import Alert, CronSchedule, MinutesSchedule

alert = Alert(
    name="MY_ALERT",
    warehouse="MY_WH",
    schedule=MinutesSchedule(minutes=15),
    condition="SELECT COUNT(*) FROM MY_TABLE WHERE STATUS = 'ERROR' AND CREATED_AT > DATEADD('hour', -1, CURRENT_TIMESTAMP())",
    action="CALL MY_SCHEMA.NOTIFY_SP()",
    comment="Managed by snowflake-provider",
)
```

---

## Schema & Database

```python
from snowflake.core.schema import Schema
from snowflake.core.database import Database

# Create database
root.databases.create(Database(name="MY_DB_SANDBOX"), mode=CreateMode.if_not_exists)

# Create schema
root.databases["MY_DB_SANDBOX"].schemas.create(
    Schema(name="MY_SCHEMA"), mode=CreateMode.if_not_exists
)
```

---

## Clone

```python
from snowflake.core import Clone, PointOfTime

# Clone a table at a point in time
root.databases["db"].schemas["schema"].tables.create(
    Table(name="MY_TABLE_BACKUP"),
    clone=Clone(source="MY_TABLE", point_of_time=PointOfTimeOffset(reference="before", when="1"))
)
```

---

## PollingOperation (async)

Every resource method has an `_async` variant returning `PollingOperation`:

```python
op = task_ref.execute_async()
result = op.result()       # blocks until complete
op.cancel()                # cancel if still running
```

---

## Gotchas

- **`create_or_alter()` is destructive on unset fields** — any field left as `None` will be set to NULL on the server. Pass the full object, always.
- **Task predecessors**: must be fully qualified (`DATABASE.SCHEMA.TASK_NAME`) when crossing schema boundaries
- **Serverless tasks**: do not set `warehouse` — setting both warehouse and serverless params raises an error
- **`fetch()` vs model**: `fetch()` returns the server-side state including computed fields (`id`, `created_on`, `state`). Don't pass these back to `create_or_alter()`.
- **`delete()` deprecated**: use `drop()` — `delete()` still works but raises a deprecation warning
- **`create_or_update()` deprecated**: use `create_or_alter()`
