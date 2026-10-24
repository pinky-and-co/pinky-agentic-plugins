# Cortex Search — Snowflake

---

## What Cortex Search is

Fully managed search service built into Snowflake. Handles both semantic (vector) and keyword (lexical) search via hybrid retrieval + reranking. No separate infrastructure, no manual embedding pipeline to maintain.

Two main use cases: RAG (retrieval-augmented generation) and enterprise search.

**What Snowflake manages automatically:**
- Embedding generation (via snowflake-arctic-embed-m-v1.5 by default)
- Index build and refresh (incremental by default)
- Hybrid retrieval (vector + BM25 lexical)
- Reranking

**What you manage:**
- Source table and its content
- TARGET_LAG (refresh frequency)
- ATTRIBUTES (filterable columns)
- Query logic in Snowpark

---

## CREATE CORTEX SEARCH SERVICE — syntax

### Single-index (one searchable column)

```sql
CREATE OR REPLACE CORTEX SEARCH SERVICE video_library_search
    ON content                              -- text column to search
    PRIMARY KEY (id)                        -- enables optimised incremental refresh
    ATTRIBUTES subject, level, source      -- filterable columns
    WAREHOUSE = my_wh                       -- dedicated warehouse recommended
    TARGET_LAG = '1 hour'                   -- max lag behind source table
    EMBEDDING_MODEL = 'snowflake-arctic-embed-m-v1.5'  -- default, can omit
    REFRESH_MODE = INCREMENTAL              -- default — requires change tracking
    INITIALIZE = ON_CREATE                  -- build index immediately
    REQUEST_LOGGING = TRUE                  -- log queries for monitoring
    AUTO_SUSPEND = 3600                     -- suspend serving after 1h inactivity
AS
    SELECT id, title, content, subject, level, source, video_url
    FROM video_library;
```

### Multi-index (multiple searchable columns — GA March 2026)

```sql
CREATE OR REPLACE CORTEX SEARCH SERVICE video_library_search
    TEXT INDEXES content, title             -- both columns searchable
    VECTOR INDEXES content(model='snowflake-arctic-embed-m-v1.5')
    PRIMARY KEY (id)
    ATTRIBUTES subject, level, source
    WAREHOUSE = my_wh
    TARGET_LAG = '1 hour'
AS
    SELECT id, title, content, subject, level, source, video_url
    FROM video_library;
```

Multi-index requires at least one VECTOR INDEXES column — TEXT INDEXES alone is not valid.

---

## Source table design — video_library example

```sql
CREATE TABLE video_library (
    id          VARCHAR NOT NULL,           -- PRIMARY KEY for incremental refresh
    title       VARCHAR,                    -- short title — searchable (multi-index)
    content     TEXT,                       -- main searchable text chunk
    subject     VARCHAR,                    -- ATTRIBUTE filter: maths, biology, history...
    level       VARCHAR,                    -- ATTRIBUTE filter: grade6, grade7, grade8, grade9
    source      VARCHAR,                    -- ATTRIBUTE filter: channel_a, channel_b, channel_c
    video_url   VARCHAR,                    -- returned in results, not searched
    tags        ARRAY                       -- optional metadata
);

-- Enable change tracking for INCREMENTAL refresh
ALTER TABLE video_library SET CHANGE_TRACKING = TRUE;
```

**Chunking strategy:** each row = one chunk. For long content (article, transcript summary),
split into chunks of ~500 tokens before inserting. Cortex Search retrieves at chunk level.

---

## Key parameters — decisions

### TARGET_LAG

How quickly the index reflects changes to the source table.

| Content type | Recommended lag |
|---|---|
| Static corpus (books, curated content) | `'1 day'` |
| Weekly batch pipeline | `'1 hour'` — runs after pipeline insert |
| Real-time support tickets | `'1 minute'` |

Short lag = more frequent refreshes = more warehouse compute cost.

### PRIMARY KEY

Strongly recommended. Without it: full refresh on every cycle (re-embed everything).
With it: incremental refresh (only changed rows re-embedded). Significant cost reduction at scale.

PRIMARY KEY columns must be VARCHAR.

### REFRESH_MODE

`INCREMENTAL` (default) — requires `CHANGE_TRACKING = TRUE` on source table and all upstream objects.
`FULL` — recomputes all embeddings every refresh. Use only if incremental is not possible.

### EMBEDDING_MODEL

Default `snowflake-arctic-embed-m-v1.5` — good baseline for French and multilingual content.
Cannot be changed after creation — recreate the service to change model.
Custom pre-computed embeddings supported (bring your own vectors via VECTOR INDEXES).

### AUTO_SUSPEND

Minimum 1800 seconds (30 minutes). Serving compute suspends after inactivity, resumes on next query.
Set to match your traffic pattern — a service used only during specific hours benefits from this.

---

## Querying — Snowflake Python Core

```python
from snowflake.core import Root
from snowflake.snowpark import Session

session = Session.builder.configs({...}).create()
root = Root(session)

search_service = (
    root.databases["my_db"]
    .schemas["my_schema"]
    .cortex_search_services["video_library_search"]
)

# Simple query
resp = search_service.search(
    query="how does photosynthesis work",
    columns=["title", "content", "source", "video_url"],
    limit=3,
)

# With attribute filter
resp = search_service.search(
    query="Pythagorean theorem right triangle",
    columns=["title", "content", "source", "video_url"],
    filter={"@eq": {"subject": "maths"}},
    limit=2,
)

# With combined filter (AND)
resp = search_service.search(
    query="causes of the French Revolution",
    columns=["title", "content", "video_url"],
    filter={"@and": [{"@eq": {"subject": "history"}}, {"@eq": {"level": "grade8"}}]},
    limit=2,
)

results = resp.results  # list of dicts
```

### Filter operators

| Operator | Usage |
|---|---|
| `@eq` | Exact match on one ATTRIBUTE |
| `@and` | Combine multiple conditions |
| `@or` | Any condition |
| `@not` | Negate a condition |
| `@in` | Match any value in a list |

---

## Monitoring

```sql
-- Enable at service creation: REQUEST_LOGGING = TRUE
-- Then query the log view
SELECT *
FROM TABLE(
    INFORMATION_SCHEMA.CORTEX_SEARCH_SERVICE_REQUEST_HISTORY(
        SERVICE_NAME => 'video_library_search'
    )
)
ORDER BY start_time DESC
LIMIT 100;
```

Useful for: query volume, latency, filter usage, detecting low-relevance queries.

---

## Cortex Search vs AI_EMBED + VECTOR_COSINE — when to use which

| | Cortex Search | AI_EMBED + VECTOR_COSINE |
|---|---|---|
| Index persistence | Managed, auto-refreshed | You manage the vector table |
| Retrieval quality | Hybrid (vector + lexical + reranking) | Vector only |
| Latency | Low (serving compute, auto-resume) | Depends on warehouse |
| Operational cost | Service + warehouse refresh | Warehouse on query |
| Best for | Production RAG, recurring queries on a corpus | One-off similarity, small datasets, custom models |

For a growing corpus with recurring queries → Cortex Search.
For one-off pairwise similarity (e.g. dedup detection in a batch job) → `AI_SIMILARITY` or `AI_EMBED`.

---

## Gotchas

- **CHANGE_TRACKING must be TRUE on source table** before creating service with REFRESH_MODE = INCREMENTAL — set it before creating the service
- **PRIMARY KEY columns must be VARCHAR** — not integer, not UUID typed
- **EMBEDDING_MODEL cannot be altered** — recreate the service to change model
- **Multi-index requires at least one VECTOR INDEXES column** — TEXT INDEXES alone fails
- **AUTO_SUSPEND minimum is 1800 seconds** (30 minutes) — cannot set lower
- **Dedicated warehouse recommended** — sharing with other workloads causes refresh contention
- **MEDIUM warehouse maximum** — same rule as all Cortex functions
- **Index build runs synchronously on CREATE with INITIALIZE = ON_CREATE** — large corpora = long CREATE statement
- **TARGET_LAG shorter than source table data retention period** — if lag > retention, service may need recreation
- **`video_url` and metadata columns** not searched — declare in SELECT of AS query, return in `columns` param of `.search()`, do not put in TEXT/VECTOR INDEXES
