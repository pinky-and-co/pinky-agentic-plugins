# Snowpark Python API

> Query `context7` first (`resolve-library-id` → `/websites/snowflake_en_developer-guide_snowpark`,
> then `query-docs`) — it serves `docs.snowflake.com/.../snowpark/` live. The site only publishes
> Snowpark docs under `/latest/` (version dropdown aside), so any snapshot — including this file —
> reflects whatever was latest at lookup time and drifts. Version lines are decoupled: the
> `snowflake` meta-package (the Snowflake Core API namespace) tracks the **core** API line, **not**
> Snowpark (its own, faster-moving line). If a deployed environment pins Snowpark loosely (e.g.
> `snowflake-snowpark-python==*`, tracking whatever the Anaconda channel currently serves), the
> live doc is a close reference for roughly that build, not a strict pin — illustratively, one
> observed pairing was a deployed `snowflake.snowpark.__version__` of 1.47.0 against a docs capture
> already at 1.48.0. For a genuinely version-borderline construct, verify against the deployed
> `snowflake.snowpark.__version__` directly rather than assuming the live doc matches exactly.

---

## API surface — overview

```
Snowpark Python
├── Session                  — entry point, connection, SQL execution
├── DataFrame                — lazy query plan (the main abstraction)
│   ├── Transformations      — select, filter, join, group_by, with_column…
│   ├── Actions              — collect, to_pandas, write.save_as_table…
│   └── Table                — DataFrame backed by a Snowflake table
├── Column + functions       — column expressions used inside DataFrames
├── Types                    — type system for UDFs, SP signatures, schemas
├── Window / WindowSpec      — window functions (rank, lag, row_number…)
├── Stored Procedures        — session.sproc (register, call)
├── UDFs / UDTFs             — session.udf / session.udtf
├── FileOperation            — session.file — stage file read/write
└── AsyncJob                 — async SP execution via session.call_nowait()
```

---

## DataFrame API vs Snowpark pandas API

**Prefer the native DataFrame API** for pipeline code.

The Snowpark pandas API (`snowflake.snowpark.modin.pandas`) lets you replace `import pandas as pd` with a Snowflake-backed version. It exists for one purpose: **migrating existing pandas code** with minimal changes. It is not a production pattern for new code.

| | DataFrame API | pandas API |
|---|---|---|
| Query plan | Explicit — you control the SQL | Opaque — translated by a compatibility layer |
| Performance | Optimal — written for Snowflake's engine | Lower — translation forces suboptimal plans and intermediate materializations |
| Predictability | High | Low — some pandas operations have no clean SQL equivalent |
| Use case | All new pipeline code | Migration bridge only |

The goal of a pandas migration is to get off the pandas API, not to stay on it.

**Exception — transformations without a clean SQL equivalent:**

Some reshaping operations are legitimately easier in pandas. The canonical case is **dynamic pivot** — Snowflake's native `PIVOT` is static (column values must be known at query-write time). For a pivot where columns are determined at runtime, the pragmatic pattern is:

```python
# Fetch the distinct pivot values first
pivot_values = [r[0] for r in df.select("category").distinct().collect()]

# Option A — dynamic SQL construction (verbose, fragile)
pivot_sql = f"SELECT * FROM ... PIVOT (SUM(amount) FOR category IN ({', '.join(repr(v) for v in pivot_values)}))"

# Option B — collect to pandas and pivot there (clean, readable)
pdf = df.to_pandas()
result = pdf.pivot_table(index="id", columns="category", values="amount", aggfunc="sum")
```

Option B is acceptable when the dataset is small enough to fit in memory. It is not a performance shortcut — it is a pragmatic response to a genuine SQL limitation.

The second pivot case where pandas wins: **pivot without aggregation**. Snowflake's `PIVOT` always requires an aggregation function. To transpose data without aggregating, the SQL workaround is `MAX()` on a column that has only one value per cell — it works but masks the intent and silently breaks if duplicates exist. pandas `df.pivot()` (not `pivot_table`) does it directly:

```python
pdf = df.to_pandas()
result = pdf.pivot(index="id", columns="category", values="value")  # no aggregation
```

**Rule of thumb for pivot decisions:**
- Static pivot, known columns, with aggregation → native Snowflake `PIVOT`
- Dynamic columns or no aggregation → collect to pandas

Other cases where pandas wins: `melt`, `stack/unstack`, complex multi-level reshaping. These are rare in pipeline code but legitimate when they appear.

---

## Session

Entry point. Inside Snowflake, never instantiate it directly — it's always received as a parameter in stored procedures.

```python
from snowflake.snowpark import Session

# Outside Snowflake (local dev / tests)
session = Session.builder.configs(CONNECTION_PARAMETERS).create()

# Inside Snowflake SP — injected by runtime
def main(session: Session, args: dict = {}) -> dict: ...
```

### Key session methods

| Method | Purpose |
|---|---|
| `session.sql(query)` | Returns a DataFrame from a SQL string — lazy, not executed until action |
| `session.table(name)` | Returns a Table DataFrame pointing to a Snowflake table |
| `session.create_dataframe(data, schema)` | Creates a DataFrame from local Python data |
| `session.call(sproc_name, *args)` | Calls a stored procedure by name synchronously |
| `session.call_nowait(sproc_name, *args)` | Calls a stored procedure asynchronously → AsyncJob |
| `session.get_current_database()` | Returns current database name |
| `session.get_current_schema()` | Returns current schema name |
| `session.get_current_role()` | Returns current role |
| `session.use_role(role)` | Switches primary role for the session |
| `session.use_schema(schema)` | Switches active schema |
| `session.write_pandas(df, table_name)` | Writes a pandas DataFrame to a Snowflake table |
| `session.file` | Returns `FileOperation` for stage file access |
| `session.sproc` | Returns `StoredProcedureRegistration` |
| `session.udf` | Returns `UDFRegistration` |
| `session.begin_transaction()` | Begins a transaction |
| `session.commit()` / `session.rollback()` | Transaction control |
| `session.query_history()` | Context manager to record queries sent to Snowflake |

---

## DataFrame API

Lazy evaluation — operations build a query plan, nothing executes until an action is called.

### Key transformations (return DataFrame)

| Method | SQL equivalent |
|---|---|
| `df.select(*cols)` | SELECT |
| `df.filter(condition)` | WHERE |
| `df.join(other, on, how)` | JOIN — `how`: inner, left, right, outer, left_anti, left_semi |
| `df.group_by(*cols).agg(...)` | GROUP BY + aggregation |
| `df.sort(*cols)` | ORDER BY |
| `df.with_column(name, col_expr)` | Add/replace a column |
| `df.drop(*cols)` | Remove columns |
| `df.union(other)` / `df.union_all(other)` | UNION / UNION ALL |
| `df.minus(other)` | MINUS (set difference) |
| `df.limit(n)` | LIMIT |
| `df.cache_result()` | Materializes to a temp table — use when consumed more than once |
| `df.rename(col, new_name)` | Column rename |

### Key actions (trigger execution)

| Method | Returns |
|---|---|
| `df.collect()` | `list[Row]` |
| `df.to_pandas()` | `pandas.DataFrame` |
| `df.count()` | `int` |
| `df.show()` | Prints first N rows |
| `df.first(n)` | `list[Row]` — first n rows |
| `df.write.save_as_table(name, mode)` | Writes to table — mode: append, overwrite, truncate, errorifexists |
| `df.write.csv(path)` | Writes to stage as CSV |

### Column expressions
```python
from snowflake.snowpark.functions import col, lit, when, sum_, count, max_, min_, avg
from snowflake.snowpark import functions as F

df.filter(col("STATUS") == lit("ACTIVE"))
df.with_column("LABEL", when(col("SCORE") > 0.8, lit("HIGH")).otherwise(lit("LOW")))
df.group_by("CATEGORY").agg(sum_("AMOUNT").alias("TOTAL"))
```

---

## Canonical vs deprecated method names

Snowpark exposes many methods under multiple names (Java-style camelCase aliases + snake_case).
Snowflake deprecated the camelCase aliases in v0.6.0 (2022) — the snake_case form is the
current, canonical API surface. Using a deprecated alias still works today but shouldn't appear
in new code.

### Column

| Canonical | Deprecated | Operation |
|---|---|---|
| `col.alias("name")` | `col.as_("name")` | Column rename |
| `col.cast(type)` | `col.astype(type)` | Type cast — SQL-native term |
| `col.bitand(other)` | `col.bitwiseAnd(other)` | Bitwise AND |
| `col.bitor(other)` | `col.bitwiseOR(other)` | Bitwise OR |
| `col.bitxor(other)` | `col.bitwiseXOR(other)` | Bitwise XOR |
| `col.equal_null(other)` | `col.eqNullSafe(other)` | Null-safe equality (`IS NOT DISTINCT FROM`) |
| `col[field]` | `col.getItem(field)`, `col.getField(field)` | ARRAY/OBJECT element access |
| `col.get_name()` | `col.getName()` | Column name string |

### DataFrame — transformations

| Canonical | Deprecated | SQL equivalent |
|---|---|---|
| `df.filter(cond)` | `df.where(cond)` | WHERE |
| `df.sort(*cols)` | `df.orderBy(*cols)` | ORDER BY — `order_by` is a different, non-deprecated method (Window spec, see below) |
| `df.minus(other)` | `df.except_(other)`, `df.subtract(other)` | MINUS / EXCEPT |
| `df.group_by(*cols)` | `df.groupBy(*cols)` | GROUP BY |
| `df.cross_join(df)` | `df.crossJoin(df)` | CROSS JOIN |
| `df.drop_duplicates(*cols)` | `df.dropDuplicates(*cols)` | DISTINCT |
| `df.with_column(n, c)` | `df.withColumn(n, c)` | ADD/REPLACE column |
| `df.with_column_renamed(o, n)` | `df.withColumnRenamed(o, n)` | RENAME column |
| `df.select_expr(*exprs)` | `df.selectExpr(*exprs)` | SELECT (SQL expr) |
| `df.approx_quantile(c, p, e)` | `df.approxQuantile(c, p, e)` | approx. quantile |

### DataFrame — output / conversion

| Canonical | Deprecated |
|---|---|
| `df.to_df(*cols)` | `df.toDF(*cols)` |
| `df.to_pandas()` | `df.toPandas()` |
| `df.to_local_iterator()` | `df.toLocalIterator()` |
| `df.print_schema()` | `df.printSchema()` |
| `df.random_split(w)` | `df.randomSplit(w)` |
| `df.sample_by(c, f)` | `df.sampleBy(c, f)` |
| `df.union_all(df)` | `df.unionAll(df)` |
| `df.union_by_name(df)` | `df.unionByName(df)` |
| `df.union_all_by_name(df)` | `df.unionAllByName(df)` |
| `df.create_or_replace_view(n)` | `df.createOrReplaceView(n)` |
| `df.create_or_replace_temp_view(n)` | `df.createOrReplaceTempView(n)` |
| `df.create_temp_view(n)` | `df.createTempView(n)` |

### Window — `order_by` is correct here, not a deprecated alias

`Window.order_by(*cols)` is the Snowpark API for window frame ordering — there is no `sort` alias
for it. Don't confuse this with `df.orderBy()` above (which *is* deprecated) — on `Window`,
`order_by` is the one and only, current form.

```python
from snowflake.snowpark import Window

window = Window.partition_by("client_id").order_by("event_date")  # correct
```

---

## SQL ↔ Snowpark translation

Canonical mapping for translating Snowflake SQL to/from the Snowpark DataFrame API. The table
reads both directions; the **pitfalls** (in Gotchas below) bite mainly going SQL → Snowpark (the
reverse is more forgiving). Import every function used — most Python linters (e.g. `ruff`) block
unused imports. For the trivial 1:1 forms (SELECT/WHERE/JOIN/GROUP BY) and camelCase→snake_case,
see "DataFrame API" and "Canonical vs deprecated method names" above — not repeated here.

> **Version-sensitive constructs** (e.g. `GROUP BY ALL`, recently-added functions) change across
> Snowpark releases. Verify them against the live doc, not from memory — query `context7`
> (`libraryId: /websites/snowflake_en_developer-guide_snowpark`) rather than trusting training data.
> For a borderline construct, confirm against the deployed `snowflake.snowpark.__version__` — the
> doc tracks whatever is latest at query time, not necessarily the exact deployed build.

| Snowflake SQL | Snowpark Python |
|---|---|
| `WHERE x IN (:list)` (temp filter table) | `.filter(col("X").isin(p_list))` |
| `LATERAL FLATTEN(col)` | `.join_table_function(flatten(col("X")))` — `flatten` from `snowflake.snowpark.functions` |
| `LATERAL FLATTEN(col, OUTER => TRUE)` | `flatten(outer=True)` is **unsupported** → `.join_table_function("flatten", coalesce(nullif(col("X"), array_construct()), array_construct(lit(""))))` (string form `"flatten"` valid; needs `nullif, array_construct, coalesce`) |
| `QUALIFY RANK() OVER (PARTITION BY A ORDER BY B DESC) = 1` | `.with_column("_RN", rank().over(Window.partition_by("A").order_by(col("B").desc()))).filter(col("_RN") == 1).drop("_RN")` |
| `GROUP BY ALL` | Check current support via `context7` before assuming a dedicated API exists — historically (Snowpark 1.4x line) `group_by` only accepted explicit `Column`/`str` (so `group_by("ALL")` would mean a *column* literally named `ALL`, not the SQL feature). Fallback if unsupported: the explicit-columns form — group by the non-aggregated columns (`df.group_by([c for c in df.columns if c not in agg_cols])`). Pre-compute derived columns with `with_column` **before** any `join_table_function`, so you never group on raw VARIANTs |
| `v:"field"::STRING` | `to_char(get(col("v"), lit("field")))` |
| `v:"a"."b"."c"::TYPE` | `get_path(col("v"), lit("a.b.c")).cast("TYPE")` |
| `arr[0]:"field"` | `get_path(get(col("arr"), lit(0)), lit("field"))` |
| `[scalar_col]` (1-element array literal) | `array_construct(col("SCALAR_COL")).cast("array")` |
| `CONVERT_TIMEZONE('Europe/Paris', col)::DATE` | `convert_timezone(lit("Europe/Paris"), col("X")).cast("date")` |
| `NULLIFZERO(x)` | `iff(col("x") == 0, lit(None), col("x"))` |
| `REPLACE(col, ',', '')` | `replace_(col("x"), lit(","), lit(""))` — `from ...functions import replace as replace_` |
| `CONCAT_WS(' ', a, b)` | `concat_ws(lit(" "), col("a"), col("b"))` — optional fields: see Gotchas |
| `col ILIKE '%pat%'` | `expr("X ILIKE '%pat%'")` or `upper(col("X")).like("%PAT%")` — `col().ilike()` does not exist |
| `COUNT(A AND B) OVER (PARTITION BY X)` | `sql_expr("COUNT(A AND B) OVER (PARTITION BY X)").alias("…")` |
| `SUM(...) / 100` | `(sum_(...) / 100).cast("decimal(16,2)")` |

---

## Type system

### Snowpark types (from `snowflake.snowpark.types`)

| Snowpark type | Snowflake SQL type | Python hint |
|---|---|---|
| `StringType()` | VARCHAR / STRING | `str` |
| `IntegerType()` | INTEGER | `int` |
| `LongType()` | BIGINT | `int` |
| `FloatType()` | FLOAT | `float` |
| `DoubleType()` | DOUBLE | `float` |
| `DecimalType(p, s)` | NUMBER(p,s) | `Decimal` |
| `BooleanType()` | BOOLEAN | `bool` |
| `DateType()` | DATE | `datetime.date` |
| `TimestampType()` | TIMESTAMP_NTZ | `datetime.datetime` |
| `TimestampType(LTZ)` | TIMESTAMP_LTZ | `datetime.datetime` |
| `TimestampType(TZ)` | TIMESTAMP_TZ | `datetime.datetime` |
| `BinaryType()` | BINARY | `bytes` |
| `VariantType()` | VARIANT | `str` (JSON) |
| `ArrayType(t)` | ARRAY | `list` |
| `MapType(k, v)` | OBJECT | `dict` |
| `StructType([fields])` | structured type / table schema | — |
| `VectorType(elem, dim)` | VECTOR | — |

### Timestamp type hints for UDFs
```python
from snowflake.snowpark.types import Timestamp, LTZ, NTZ, TZ

def my_udf(ts: Timestamp[NTZ]) -> str: ...   # TIMESTAMP_NTZ
def my_udf(ts: Timestamp[LTZ]) -> str: ...   # TIMESTAMP_LTZ
```

---

## Stored Procedures — registration

```python
# Via decorator
@session.sproc(return_type=StringType(), input_types=[StringType()])
def my_sp(session: Session, arg: str) -> str: ...

# Via registration object
session.sproc.register(func=my_sp, return_type=StringType(), input_types=[...], name="MY_SP")

# From file
session.sproc.register_from_file("path/to/sp.py", "main", name="MY_SP")
```

---

## User-Defined Functions (UDFs)

### Scalar UDF
```python
@session.udf(return_type=StringType(), input_types=[StringType()])
def clean_date(raw: str) -> str:
    if len(raw) == 5 and raw[0] == "2": return raw[:4]  # 21022 → 2102 heuristic
    return raw

# Apply on DataFrame
df.with_column("CLEAN_DATE", clean_date(col("RAW_DATE")))
```

### Vectorized UDF (batch processing — better performance)
```python
from snowflake.snowpark.types import PandasSeries, PandasSeriesType
import pandas as pd

@session.udf(return_type=StringType(), input_types=[PandasSeriesType(StringType())])
def clean_date_vectorized(series: PandasSeries) -> PandasSeries:
    return series.apply(lambda v: v[:4] if len(v) == 5 else v)
```

Vectorized UDFs operate on pandas Series/DataFrame batches — significantly more efficient for string/numeric processing at scale.

### UDTF (Table Function)
Returns multiple rows per input row. Register via `session.udtf.register()`.

---

## SnowflakeFile — Stage file access in SPs/UDFs

```python
from snowflake.snowpark.files import SnowflakeFile

# Read a file from stage (inside SP or UDF)
with SnowflakeFile.open(file_url, 'rb') as f:
    data = f.read()

# Write result file
with SnowflakeFile.open_new_result() as f:
    f.write(output_bytes)
```

- `file_url`: stage URL in format `@stage_name/path/file.ext`
- `is_owner_file=True`: for files owned by the SP owner (not the caller)
- Common uses: decrypting stage files, parsing Excel/CSV uploads inside a handler

---

## AsyncJob

```python
# Launch SP asynchronously
job = session.call_nowait("MY_SP", args)

# Check status
job.is_done()     # bool
job.is_failed()   # bool
job.status        # str

# Get results (blocks until done)
result = job.result()

# Or convert to DataFrame
df = job.to_df()
```

---

## Gotchas

- **Lazy evaluation**: `df.filter(...)` does not execute SQL — only `.collect()`, `.to_pandas()`, `.write.*` etc. trigger execution
- **Column name case**: Snowflake columns are case-insensitive by default; use `col("NAME")` not `df.NAME` to avoid surprises
- **`cache_result()` — performance**: materializes to a temp table — use when a DataFrame is consumed more than once in the same SP to avoid re-computation
- **`cache_result()` — CORRECTNESS after a window then a filter**: a window function (`rank`/`row_number`/`count`/`sum … .over(…)`) **must be computed over the full dataset before any narrowing `.filter()`**. Under lazy evaluation the filter can fold into the window's scope → the frame is computed on the filtered subset = wrong `COUNT`/`RANK`. Insert `.cache_result()` between the window `.select(...)` and the filter. Real case: `df_all = t.select(…, count(...).over(partition_by("GROUP_ID")).alias("N")).cache_result()` **then** `df = df_all.filter(col("SUB_GROUP_ID").isin(p_ids))` — without `cache_result()`, `N` counts only within the filtered ids
- **`left_anti` join**: prefer over `NOT IN` / `NOT EXISTS` subquery for "rows not in other table" patterns
- **`lit()` for constants**: always use `lit("value")` in column expressions, never bare Python strings
- **Session inside Snowflake**: never call `Session.builder.create()` inside a SP — the session is injected by the runtime
- **Package imports**: lazy imports inside functions for utility modules — only `main()` and direct SP handlers import at top level, to keep handler cold-start light
- **Timestamp timezone**: default `TimestampType()` = NTZ — use `TimestampType(LTZ)` explicitly for timezone-aware data
- **Column naming after multi-source joins**: pick a consistent per-source column-naming convention (a prefix, an alias scheme — whatever fits your codebase) so post-join column references stay unambiguous across chained joins; the specific scheme is a house-style choice, not a Snowpark requirement. Pre-compute derived columns (`with_column`) **before** a `join_table_function`/`group_by`, and never group on a raw VARIANT (group on a scalar key + pre-computed columns instead)
- **Annotate intermediate DataFrames** `: snowpark.DataFrame` for editor/type-checker inference

### Translation pitfalls (SQL → Snowpark)

- **VARIANT before `.isin`**: cast to string first — `to_char(get(col("V"), lit("type"))).isin("A", "B")`, never `get(...).isin(...)` raw
- **Typed conversions over `.cast("…")`**: extract VARIANT scalars with `to_decimal(x, p, s)` / `to_decfloat(x)` / `to_boolean(x)` (and `to_char`), **not** `.cast("decimal(…)")` / `.cast("float")` / `.cast("boolean")`. The function form avoids scale traps — e.g. `.cast("decimal(38,0)")` **truncates** the fractional part before a `/100`; use `to_decimal(get_path(...), 30, 10)` then `/ 100` then `to_decimal(…, 16, 2)`
- **`concat_ws` with an optional field**: wrap the optional field in `coalesce(to_char(get(col("V"), lit("f"))), lit(""))` inside the `concat_ws` (the separator space is added by `concat_ws` itself). Never `lit(" ") + to_char(col("x"))` (invalid expr) and no `with_column` pre-step needed
- **`startswith` argument**: must be a column/lit — `col("X").startswith(lit("FA"))` or `col("X").like("FA%")`
- **`year(col)` & friends**: not always resolved from `snowflake.snowpark.functions` — use `sql_expr("YEAR(X)")` or import the function explicitly and verify it exists
- **`.isin(...)` returns a boolean Column**: wrap in `iff` when a business value is expected — `iff(col("X").isin("A", "B"), lit("oui"), lit("non")).alias("Y")`
- **camelCase on `Column`/`Window`**: the whole API is snake_case — see "Canonical vs deprecated method names" above; a deprecated alias still runs today but shouldn't appear in new code

---

## Exception handling — `snowflake.snowpark.exceptions`

Always catch Snowpark-specific exceptions instead of bare `Exception`. This surfaces real bugs
(typos, missing imports) instead of silently swallowing them.

### Available exceptions

```python
from snowflake.snowpark.exceptions import (
    SnowparkSQLException,                  # SQL error (permissions, syntax, missing object)
    SnowparkSessionException,              # Session expired / disconnected / invalid token
    SnowparkDataframeException,            # DataFrame construction error (bad column ref)
    SnowparkInvalidObjectNameException,    # Invalid object name in a DataFrame operation
    SnowparkColumnException,               # Ambiguous or missing column
    SnowparkJoinException,                 # Ambiguous join (column name conflicts)
    SnowparkPlanException,                 # Plan too complex / invalid
    SnowparkCreateDynamicTableException,   # Dynamic table creation error
    SnowparkUploadFileException,           # PUT / stage upload failure
)
```

### Key attributes on `SnowparkSQLException`

```python
except SnowparkSQLException as e:
    e.message          # full error message
    e.sql_error_code   # Snowflake numeric code (2003=not exists, 3001=insufficient privileges)
    e.raw_message      # raw message without Snowpark formatting
```

### When to catch what

| Context | Catch | Let crash |
|---|---|---|
| UDF / UDTF handler | Nothing (or log + re-raise) | Everything — Snowflake runtime handles it |
| SP orchestration | `SnowparkSQLException` (retry / fallback / meaningful error) | `SnowparkSessionException` (fatal) |
| Streamlit app | `SnowparkSessionException` → `st.stop()` | `SnowparkSQLException` → display to user |
| Provider (IaC) | `SnowparkSQLException` → diff / plan reporting | Runtime errors → crash CI pipeline |
| External-API integration (EAI) | `SnowparkSQLException` for the SQL layer; the HTTP client's own exceptions for the HTTP layer | Unexpected errors → log + raise |

### Pattern — SP with SQL error handling

```python
from snowflake.snowpark.exceptions import SnowparkSQLException

def main(session, client_schema: str):
    try:
        session.sql(f"SELECT 1 FROM {client_schema}.CONFIG LIMIT 1").collect()
    except SnowparkSQLException as e:
        if e.sql_error_code == 2003:  # object does not exist
            raise ValueError(f"Client schema not provisioned: {client_schema}") from e
        if e.sql_error_code == 3001:  # insufficient privileges
            raise PermissionError(f"Missing grant on {client_schema}") from e
        raise
```

### Pattern — Streamlit session guard

```python
from snowflake.snowpark.exceptions import SnowparkSessionException

try:
    result = session.sql("SELECT CURRENT_ROLE()").collect()
except SnowparkSessionException:
    st.error("Session expired — please refresh the page.")
    st.stop()
```

### Useful SQL error codes (non-exhaustive)

| Code | Meaning |
|---|---|
| 904 | Invalid identifier |
| 2003 | Object does not exist or not authorized |
| 2043 | Object already exists |
| 3001 | Insufficient privileges |
| 90105 | Cannot perform operation (object locked) |
| 100035 | Statement timed out |
