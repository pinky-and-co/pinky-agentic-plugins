# Cortex AI Functions — Snowflake

---

## Function map — what exists and what it covers

| Function | Input | Output | Primary use case |
|---|---|---|---|
| `AI_EXTRACT` | text or file | JSON object | Structured extraction from documents |
| `AI_PARSE_DOCUMENT` | file | raw text (pages) | Full text extraction for RAG / indexing |
| `AI_TRANSCRIBE` | audio/video file | text + timestamps + speakers | Speech-to-text |
| `AI_TRANSLATE` | text | text | Language translation |
| `AI_REDACT` | text | text | PII anonymisation |
| `AI_SENTIMENT` | text | sentiment score | Sentiment analysis |
| `AI_CLASSIFY` | text or image | category label(s) | N-label classification |
| `AI_FILTER` | text or image | boolean | Natural language WHERE clause — and guardrails |
| `AI_AGG` | text column | insight text | Multi-row aggregation — no context window limit |
| `AI_SUMMARIZE_AGG` | text column | summary text | Multi-row summarisation — no context window limit |
| `AI_EMBED` | text or image | vector | Embedding for RAG, clustering, similarity |
| `AI_SIMILARITY` | text or image × 2 | float 0–1 | Pairwise similarity without storing vectors |
| `AI_COMPLETE` | text or image | text | General LLM completion — universal fallback |
| `AI_COUNT_TOKENS` | function_name (`'ai_*'`), text, model (if the target function needs one) | integer | Token count before calling an AI function. |
| `TO_FILE` | stage path | FILE reference | Stage file reference for file-input functions |

---

## When multiple functions cover the same use case

| Use case | Candidates | Decision rule |
|---|---|---|
| Summarise a single text | `AI_COMPLETE`, `AI_SUMMARIZE_AGG` | `AI_COMPLETE` for one row with a precise prompt. `AI_SUMMARIZE_AGG` for multiple rows — no context window limit. |
| Summarise multiple rows | `AI_AGG`, `AI_SUMMARIZE_AGG` | `AI_SUMMARIZE_AGG` for free-text summary. `AI_AGG` for a specific question across rows. |
| Extract from a document | `AI_EXTRACT`, `AI_PARSE_DOCUMENT` + `AI_COMPLETE` | Always `AI_EXTRACT` directly on the file. `AI_PARSE_DOCUMENT` only when you need the raw full text (RAG indexing, `AI_EMBED` pipeline). |
| Sentiment | `AI_SENTIMENT`, `AI_COMPLETE` | `AI_SENTIMENT` — cheaper, batch-optimised. `AI_COMPLETE` only for aspect-based or nuanced prompts. |
| Classification | `AI_CLASSIFY`, `AI_FILTER`, `AI_COMPLETE` | `AI_CLASSIFY` for N defined categories (up to 500). `AI_FILTER` for binary true/false in a WHERE clause or guardrail. `AI_COMPLETE` for complex or dynamic rules. |
| Similarity | `AI_SIMILARITY`, `AI_EMBED` + VECTOR_COSINE | `AI_SIMILARITY` for one-off pairwise scoring. `AI_EMBED` + Cortex Search when building a persistent searchable index. |
| Translation | `AI_TRANSLATE`, `AI_COMPLETE` | `AI_TRANSLATE` — simpler, cheaper, purpose-built. `AI_COMPLETE` only when translating and reformatting simultaneously. |
| Content moderation / guardrails | `AI_FILTER`, Cortex Guard, Cortex AI Guardrails | See guardrails section below — three distinct mechanisms. |

---

## AI_EXTRACT — the critical function

### What it supports

- **File formats**: PDF, PNG, PPTX/PPT, EML, DOC/DOCX, JPEG/JPG, HTM/HTML, TXT, TIF/TIFF, BMP, GIF, WEBP, MD
- **Max file size**: 100 MB
- **Max pages**: 125 (digital-born or scanned)
- **Languages**: 29 including French, English, German, Spanish, Italian, Portuguese, Arabic, Chinese, Japanese, Korean
- **Model**: arctic-extract — a 6.6 GiB Vision Language Model (VLM). Processes document as image+text natively — no prior OCR step needed. Optimised for both digital-born and scanned documents.

### Extraction modes in a single call

| Mode | Schema trigger | Output |
|---|---|---|
| Entity | `'type': 'string'` on sub-object | Single value per field |
| List | `'type': 'array'` on sub-object | Array of strings |
| Table | `'type': 'object'` with `column_ordering` | Object of arrays (columns) |
| Combined | Mix of the above | All types in one response |

### Supported scalar types in responseFormat

Only `"type": "string"` and `"type": "array"` are valid for leaf nodes.
`"date"`, `"integer"`, `"decimal"`, `"boolean"` are **not supported** — silently ignored or cause errors.

| Desired type | Declare as | Post-process in SQL |
|---|---|---|
| date | `"string"` | `TRY_TO_DATE(col::string, 'YYYY-MM-DD')` |
| integer | `"string"` | `TRY_CAST(col::string AS INT)` |
| decimal | `"string"` | `TRY_CAST(col::string AS DECIMAL)` |
| boolean | `"string"` | `CASE WHEN col::string = 'oui' THEN TRUE ...` |

### Absent fields — correct behaviour

When the model cannot find a value, **the key is absent from the response JSON** — not null, not empty string.
Reading from a Snowflake VARIANT column: an absent key path returns NULL automatically.

```python
# Snowpark — safe read
result['response'].get('soc_tva')   # → None if absent, no KeyError

# SQL — safe read
result:response:soc_tva::string     -- → NULL if key absent, no error
```

Do not instruct the model to return `"null"` or `"N/A"` — it adds noise to handle downstream.

### scores parameter

```sql
AI_EXTRACT(
    file           => TO_FILE('@stage', relative_path),
    responseFormat => {...},
    scores         => TRUE   -- optional, default FALSE, no extra cost
)
```

Returns a `scoring` object alongside `response`. Each field gets a score 0–1 indicating model confidence.
Use scores to route low-confidence extractions to a human review table.
Per-element scores for individual array items and table cells are **not available** — aggregate score per field only.

Post-extraction validation pattern:

```python
# regex guard + score guard → route to review table
import re
NAF_RE = re.compile(r'^\d{4}[A-Z]$')

score = result['scoring']['scores'].get('soc_naf', {}).get('score', 0)
value = result['response'].get('soc_naf', '')

if score < 0.8 or not NAF_RE.match(value):
    # INSERT INTO review_table
```

### scale_factor — scanned documents

For scanned documents, degraded quality, small text, or dense layouts.

| scale_factor | Tokens per page | Max pages |
|---|---|---|
| 1.0 (default) | 970 | 125 |
| 2.0 | 1 940 | 62 |
| 2.5 | 2 425 | 50 |
| 4.0 | 3 880 | 31 |

```sql
AI_EXTRACT(
    file   => TO_FILE('@stage', 'scan.pdf'),
    responseFormat => {...},
    config => {'scale_factor': 2.0}
)
```

### Cost

Billed on input tokens (970 per page × scale_factor) + output tokens.
Warehouse size: **MEDIUM maximum** — larger warehouses do not improve performance.

---

## responseFormat — best practices

### Rule 1 — Always include an inline example

The example does more work than the label. arctic-extract mirrors the format of the example provided.

```json
"soc_rcs": {
    "description": "Full RCS registration number including court city. e.g. 'Paris B 123 456 789'",
    "type": "string"
}
```

### Rule 2 — Format constraints in plain language — no regex

arctic-extract does not interpret regex in descriptions. Plain rule + example is the correct pattern.

```json
"soc_naf": {
    "description": "APE/NAF code. 4 digits + 1 uppercase letter, no dot separator. e.g. '6920Z' not '69.20Z'",
    "type": "string"
}
```

Regex belongs in post-extraction SQL/Python validation, not in the description.

### Rule 3 — Suppress inference explicitly

For fields that could be computed or deduced from other fields:

```json
"soc_duree": {
    "description": "Company duration in whole years. Only extract if explicitly stated as a number in the document. e.g. '99'",
    "type": "string"
}
```

### Rule 4 — For arrays, define separator and field order with a concrete example

```json
"gerants": {
    "description": "One entry per person as: 'CIVILITY FIRSTNAME LASTNAME | full home address | exact role title as in document'. e.g. 'M. Laurent CLAUDE | 4 bis Rue Ernest Renan 33600 Pessac | Président'",
    "type": "array"
}
```

### Rule 5 — For normalised values, give the target list

```json
"soc_forme": {
    "description": "Legal form. Must be exactly one of: SAS, SASU, SARL, EURL, SA, SNC, SCI, GIE, SELARL, SELAS. Use the short form only.",
    "type": "string"
}
```

### Rule 6 — Limits

Max 100 entity extraction questions per call.
Table extraction question = 10 entity questions → max 10 table questions per call.

---

## AI_PARSE_DOCUMENT — when to use instead of AI_EXTRACT

`AI_PARSE_DOCUMENT` is **not** a pre-processing step for `AI_EXTRACT`.

| Use case | Why AI_PARSE_DOCUMENT |
|---|---|
| RAG pipeline — chunking + embedding | Need raw text per page to split into chunks for `AI_EMBED` or Cortex Search |
| Full-text search indexing | Need complete document text, not specific fields |
| Feeding `AI_AGG` or `AI_SUMMARIZE_AGG` | Need full text as aggregation input |

For structured field extraction → always use `AI_EXTRACT` directly on the file.

---

## Guardrails — three distinct mechanisms

Three mechanisms exist in Cortex. They are not interchangeable.

| | Cortex Guard | AI_FILTER | Cortex AI Guardrails |
|---|---|---|---|
| What it is | Option on `AI_COMPLETE` | Standalone SQL function | Account-level Snowflake Horizon feature |
| Activation | `guardrails => TRUE` on `AI_COMPLETE` | Explicit call in your pipeline | `ALTER ACCOUNT SET AI_SETTINGS` |
| Model | Llama Guard 3 | arctic-extract VLM | Dedicated safety model |
| Categories | Fixed (violence, hate, self-harm…) | Free — you write the condition in natural language | Prompt injection + jailbreak detection |
| Direction | Output of `AI_COMPLETE` only | Input AND output — your choice | Input |
| Loggable in Snowflake | No | Yes — INSERT into any table | Yes — conversation logs (Cortex Agents only, not Cortex Code) |
| Configurable | No | Fully — prompt is yours | Limited to on/off per account |
| Snowflake edition | All | All | Enterprise only |
| Cost | Included | Tokens consumed | Tokens scanned — separate pricing |
| Best for | Baseline content safety on LLM output | Business-logic guardrails, bidirectional, loggable | Anti-jailbreak / prompt injection protection |

**Use all three together for a production conversational assistant with domain restrictions:**
- Cortex AI Guardrails (Enterprise) → blocks jailbreak attempts at account level
- Cortex Guard (`guardrails => TRUE`) → baseline content safety on LLM response
- `AI_FILTER` × 2 → bidirectional business-logic guardrail (e.g. "stay within the assistant's declared scope"), loggable in `guardrail_logs`

**Observability — critical distinction:**

| | Cortex Code | Cortex Agents |
|---|---|---|
| What it is | AI coding assistant in Snowsight / CLI | Applicative agents via API |
| Conversation logs stored in Snowflake | **No** — stored locally in `~/.snowflake/cortex/conversations/` | **Yes** — `SNOWFLAKE.LOCAL.AI_OBSERVABILITY_EVENTS` |
| Private mode | `cortex --private` disables local session saving | N/A |
| Unredacted content access | N/A | Requires `READ UNREDACTED AI OBSERVABILITY EVENTS TABLE` account privilege |
| Without that privilege | N/A | Metadata only: tool names, token usage, latency, model name |

`guardrail_logs` (your `AI_FILTER` table) is always in Snowflake regardless of runtime — it is your own table, not an observability system. It provides: timestamp, session_id, direction (INPUT/OUTPUT), content, and constitutes the legal proof that guardrails were active.

---

## Typical pipelines

**Structured extraction (KBIS / statuts / liasse fiscale)**
```
TO_FILE(@stage, path)
  → AI_EXTRACT(file, responseFormat, scores => TRUE)
  → validate: scores + regex post-extraction
  → route: score >= threshold → persistent table
         | score <  threshold → review table
```

**RAG pipeline**
```
AI_PARSE_DOCUMENT(file, 'LAYOUT')
  → AI_REDACT (if PII)
  → chunk text
  → INSERT INTO source_table (for Cortex Search indexing)
  → Cortex Search Service (managed embeddings + hybrid retrieval)
  → query: search_service.search(query, columns, filter, limit)
  → AI_COMPLETE(context_chunks + question)
```

**Multi-document aggregation**
```
AI_TRANSLATE (normalise language)
  → AI_CLASSIFY (category)
  → AI_SENTIMENT (score)
  → AI_SUMMARIZE_AGG (GROUP BY category)
```

**Guard before AI_COMPLETE**
```
AI_COUNT_TOKENS('ai_complete', text, model => model)  -- verify fits in context window
  → TO_FILE(@stage, path)   -- file reference for document input
  → AI_COMPLETE(model, prompt, guardrails => TRUE)  -- Cortex Guard baseline
  → AI_FILTER (output)      -- business-logic guardrail on top
```

---

## Fine-tuning arctic-extract

### When fine-tuning is justified

Fine-tuning improves **consistency and normalisation**, not raw accuracy on simple documents.
Zero-shot is sufficient for standard, well-structured, digital-born documents with clear labels.

| Signal | Example |
|---|---|
| Output format inconsistent across documents | `soc_cap` returns "1 080 060 €" on some docs, "1080060.00" on others |
| Model infers values it should leave absent | `soc_date_fin` computed instead of read |
| Controlled vocabulary not respected | `soc_forme` returns full legal name despite short-form instruction |
| Complex multi-level tables | Cerfa fiscal forms (2050, 2051, 2052) with nested headers |
| Scanned documents with variable quality at scale | High error rate even with scale_factor tuning |
| Industrial volume + SLA | Batch processing requiring < 1% error rate |

Fine-tuning does **not** reduce cost — billing is identical for fine-tuned and base models.

### Dataset requirements

| Constraint | Value |
|---|---|
| Minimum documents recommended | 20 |
| Maximum unique document files | 1 000 |
| Max pages per document | 64 (AWS Oregon / Frankfurt) or 125 (AWS Virginia / Azure Virginia East US 2) |
| Hard constraint | questions × total pages ≤ 50 000 |
| Supported file formats | PDF, PNG, JPG/JPEG, TIF/TIFF |
| Required columns | `File` (stage path string), `Prompt` (responseFormat JSON), `Response` (expected output JSON) |

The `Response` column teaches normalisation — must be **perfectly consistent** across all examples.
If document does not contain an answer: **omit the key entirely** from the Response JSON (same as inference behaviour).

Fine-tuning preview region availability changes as the feature moves toward GA — verify current regions via `context7` rather than trusting a fixed list here.

### Fine-tuning workflow — Snowpark Python

```python
from snowflake.snowpark import Session
from snowflake.ml import dataset

session = Session.builder.configs({...}).create()

# 1. Training data table — TO_FILE('@stage', relative_path) as a per-row SQL expression
#    is the practical way to build FILE-typed rows in bulk. The native Python `to_file()`
#    wrapper (snowflake.snowpark.functions.to_file) takes a plain Python string, not a
#    Snowpark Column — it can't be mapped over a DataFrame's path column the way the SQL
#    form can. Deliberate exception to "prefer the Snowpark function": SQL is genuinely
#    the better tool for bulk, per-row file references.
session.sql("""
    CREATE TABLE IF NOT EXISTS ft_training (f FILE, p VARCHAR, r VARCHAR)
""").collect()

# 2. Insert labelled examples — Response must be perfectly consistent
session.sql("""
    INSERT INTO ft_training VALUES (
        TO_FILE('@doc_stage', 'kbis_001.pdf'),
        '{
            "soc_nom": "Exact legal company name (denomination sociale)",
            "soc_forme": "Legal form, exactly one of: SAS, SASU, SARL, EURL, SA"
        }',
        '{
            "soc_nom": "SAS PALMERAIE",
            "soc_forme": "SAS"
        }'
    )
""").collect()

# 3. Build the Dataset directly from a Snowpark DataFrame — snowflake.ml Python API,
#    no CREATE DATASET / ALTER DATASET ADD VERSION SQL needed
training_df = session.sql("""
    SELECT FL_GET_STAGE(f) || '/' || FL_GET_RELATIVE_PATH(f) AS "file",
           p AS "prompt",
           r AS "response"
    FROM ft_training
""")
ds = dataset.create_from_dataframe(
    session,
    "ft_dataset",
    "v1",
    input_dataframe=training_df,
)

# 4. Launch fine-tuning job — SNOWFLAKE.CORTEX.FINETUNE has no Python/Snowpark
#    wrapper (only the generic Snowflake Python APIs entry point, not a dedicated one):
#    session.sql is the legitimate, documented way to call it.
result = session.sql("""
    SELECT SNOWFLAKE.CORTEX.FINETUNE(
        'CREATE',
        'my_db.my_schema.kbis_extractor',
        'arctic-extract',
        'snow://dataset/my_db.my_schema.ft_dataset/versions/v1'
    )
""").collect()

# 5. Monitor — same reason, session.sql only
session.sql("""
    SELECT SNOWFLAKE.CORTEX.FINETUNE('DESCRIBE', '<job_id_from_step_4>')
""").collect()
```

`dataset.create_from_dataframe(session, name, version, input_dataframe=df)` materializes a Snowpark
DataFrame straight into a new, versioned Dataset — this replaced the raw `CREATE OR REPLACE DATASET` +
`ALTER DATASET ... ADD VERSION ... FROM (...)` pair below. `snowflake.ml.dataset.Dataset` also exposes
`.create_version()` for adding further versions to an existing Dataset object. Verify current signature
via `context7` (`libraryId: /websites/snowflake_en`, module `snowflake.ml.dataset`) before relying on
it — this is a `snowflake-ml-python` API, released independently from `snowpark-python`, so the two
package versions can drift out of sync.

### Inference with fine-tuned model

Validated pattern — a single-value helper handling both file and plain-text input:

```python
from snowflake.snowpark.functions import ai_extract, lit, to_file


def extract(session, source: str, response_format: dict, from_file: bool = False):
    input_col = to_file(source) if from_file else lit(source)
    row = session.range(1).select(ai_extract(input_col, response_format)).collect()[0]
    return row[0]


extract(session, "@doc_stage/kbis_001.pdf", response_format, from_file=True)
extract(session, "some raw text to extract from", response_format, from_file=False)
```

`session.range(1).select(...)` forces evaluation as a single row — this calls `AI_EXTRACT` once per
file or text, it does not batch across a whole directory in one query. For volume, loop this per file
in Python, or fall back to the SQL two-arg form for a single query over a directory:
`SELECT AI_EXTRACT(model => ..., file => TO_FILE('@stage', relative_path), scores => TRUE) FROM DIRECTORY(@stage)`
— SQL's `TO_FILE('@stage', relative_path)` accepts `relative_path` as a column, which is exactly what
the Snowpark `to_file()` wrapper cannot do (see gotcha below).

Whether `ai_extract()` exposes `model=` / `scores=` as keyword arguments the same way the SQL function
does is worth confirming with `help(ai_extract)` or `context7` — not shown in the pattern above because
it wasn't verified against a real call.

Fine-tuned model can be copied between schemas and accounts: `ALTER MODEL ... COPY TO`.
Number of epochs: not configurable — determined automatically by Snowflake.

---

## Snowpark Python — native functions vs session.sql

**All Cortex AI functions** have a native Column wrapper in `snowflake.snowpark.functions`:

```python
from snowflake.snowpark.functions import (
    ai_agg,
    ai_classify,
    ai_complete,
    ai_count_tokens,
    ai_embed,
    ai_extract,
    ai_filter,
    ai_parse_document,
    ai_redact,
    ai_sentiment,
    ai_similarity,
    ai_summarize_agg,
    ai_transcribe,
    ai_translate,
)
```

`ai_count_tokens` and `ai_redact` got native wrappers in snowpark-python 1.54.0 (2026-07-29) — before
that release, both required `call_builtin`. For anything still lacking a wrapper (check `dir(functions)`
first — the list keeps growing), fall back to `call_builtin`:

```python
from snowflake.snowpark.functions import call_builtin, lit

session.range(1).select(
    call_builtin("VECTOR_COSINE_SIMILARITY", lit(v1), lit(v2))
).collect()
```

Token counting: use `AI_COUNT_TOKENS` — GA since 2026-01-27, the canonical function (native Snowpark
wrapper `ai_count_tokens` since 1.54.0). Its `function_name` argument must start with `ai_` (lowercase,
e.g. `'ai_complete'`) and it does **not** work against `SNOWFLAKE.CORTEX.*` functions or fine-tuned
models — text input only (no image/audio/video). Legacy `SNOWFLAKE.CORTEX.COUNT_TOKENS(model, text)`
still runs but is slated for deprecation by end of 2026.

**History — a real bug, not just a stale note:** on 2026-07-14 we verified live that token counting
returned `NULL` and rejected `claude-*` models. That was a genuine bug at the time — `DataFrame.ai.count_tokens`
was still routing through the legacy `SNOWFLAKE.CORTEX.COUNT_TOKENS`, which doesn't support Claude the
same way `AI_COMPLETE` does. snowpark-python 1.54.0 (2026-07-29) fixed the routing to call `AI_COUNT_TOKENS`
directly. Current Snowflake docs confirm `AI_COUNT_TOKENS('ai_complete', 'claude-sonnet-4-5', text, ...)`
works. Remaining caveat: for Claude models called with `response_format` (structured output), `AI_COMPLETE`
generates extra request content that's billed as input tokens but **not** reflected in the `AI_COUNT_TOKENS`
estimate — billed input tokens can run materially higher than the estimate for those calls specifically.

**Rule: never use `session.sql("SELECT AI_*(...)")` when a Snowpark function exists.**
The only legitimate `session.sql()` usage is DDL/admin commands with no Python equivalent.

---

## Warehouse sizing

**MEDIUM maximum** for all Cortex AI functions and Cortex Search services.
Larger warehouses do not improve performance and generate unnecessary compute cost.
Use a **dedicated warehouse** for Cortex Search services — do not share with other workloads.

---

## Caching Cortex calls — content-addressed table cache (cost control)

Cortex calls (`ai_complete`, `AI_EXTRACT`, …) cost credits per call. When the same input recurs, cache
the **result in a Snowflake table keyed by a content hash** — the call then fires only on genuinely new
input. Pattern validated in production (a SiS report-hub generating an AI description per procedure):

```python
code_hash = hashlib.md5(code.encode()).hexdigest()
cached = session.sql(
    f"SELECT RESULT FROM {CACHE} WHERE CODE_HASH = ?", params=[code_hash]
).collect()
if cached and cached[0][0]:
    return json.loads(str(cached[0][0]))  # hit → zero Cortex cost
# miss → call ai_complete, then persist
result_json = json.dumps(result, ensure_ascii=False)
session.sql(
    f"""MERGE INTO {CACHE} t USING (SELECT ? CODE_HASH, ? RESULT) s ON t.CODE_HASH = s.CODE_HASH
        WHEN NOT MATCHED THEN INSERT (CODE_HASH, RESULT, CREATED_AT) VALUES (s.CODE_HASH, s.RESULT, CURRENT_TIMESTAMP())""",
    params=[code_hash, result_json],
).collect()
```

- **Why a table, not just `st.cache_*`** — in SPCS / container warehouse the in-memory cache dies on
  cold start (deploy / new node); the table survives. See `AGENT_SNOWFLAKE_STREAMLIT.md`.
- **`MERGE`** on insert avoids duplicate rows under concurrent cold-start races.
- **Auto-invalidation** — a content change → new hash → miss → regenerate. No manual eviction.
- **Robust JSON parse** — Cortex sometimes double-encodes (outer JSON string) or wraps in a ```` ```json ````
  block despite instructions; unwrap before `json.loads`, and keep a fallback on parse failure.

### Exact-hash vs similarity cache (open tradeoff)

- **Exact `MD5` hash (proven)**: deterministic, zero false hits; reuses only on identical input. Right
  when correctness is non-negotiable (code → explanation: a near-identical input must NOT reuse).
- **Similarity cache (e.g. embeddings + threshold — the `pinky-ai` lib's theoretical approach)**: more
  hits across *similar* inputs, but risks **false positives** (returning a result for a not-quite-equal
  input). Still to validate against the proven exact-hash app before adopting. Default to exact-hash
  unless the hit-rate gain is measured AND the false-positive cost is acceptable.

---

## Gotchas

- **`"type": "date"` / `"integer"` / `"decimal"` in responseFormat are not supported** — always use `"string"`, cast post-extraction in SQL
- **Regex in `description` does not work** — plain language rule + inline example only
- **Absent field = absent key in JSON** — do not instruct the model to return "null" or "N/A"
- **Fine-tuning does not reduce cost** — quality and consistency only
- **`scale_factor` reduces max pages** — 2.0 → 62 pages max, 4.0 → 31 pages max
- **Array scores are aggregate** — one score per array field, not per element
- **Three distinct guardrail mechanisms** — Cortex Guard (fixed categories, AI_COMPLETE output only), AI_FILTER (free prompt, bidirectional, loggable, all editions), Cortex AI Guardrails (anti-jailbreak, account-level, Enterprise only). See guardrails section.
- **Cortex Code conversations are NOT in Snowflake** — stored locally in `~/.snowflake/cortex/conversations/`. Only Cortex Agents conversations are logged in `SNOWFLAKE.LOCAL.AI_OBSERVABILITY_EVENTS`.
- **Fine-tuning preview regions are subject to change** — verify current list via `context7`, do not trust a fixed region list.
- **`SNOWFLAKE.CORTEX.COUNT_TOKENS` is legacy, slated for deprecation by end of 2026** — use `AI_COUNT_TOKENS` instead (GA 2026-01-27, native `ai_count_tokens` Snowpark wrapper since snowpark-python 1.54.0 / 2026-07-29). `AI_COUNT_TOKENS` only accepts `function_name` values starting with `ai_` — it does not work against `SNOWFLAKE.CORTEX.*` functions or fine-tuned models. This was a real bug we hit in prod (2026-07-14: `NULL` results, `claude-*` rejected) caused by `DataFrame.ai.count_tokens` still routing through the legacy function — fixed in 1.54.0, not just a stale note. For Claude + `response_format` (structured output), `AI_COUNT_TOKENS` still underestimates — extra request content is billed but not counted.
- **Snowpark `to_file()` takes a plain Python string, not a Snowpark Column** — a variable is fine (`to_file(source)` where `source` is a Python `str` works), what it can't take is a DataFrame column reference that would let the value vary per row within a single query. That's why the validated pattern calls `session.range(1).select(ai_extract(to_file(path), ...))` once per file rather than mapping `to_file()` over a column of paths. For a one-query-per-directory batch, the SQL two-arg `TO_FILE('@stage', relative_path)` (column-friendly) inside `session.sql(...) FROM DIRECTORY(@stage)` is still the right tool.
- **Dataset creation/versioning has its own Python API** — `snowflake.ml.dataset.create_from_dataframe(session, name, version, input_dataframe=df)` replaces the raw `CREATE OR REPLACE DATASET` + `ALTER DATASET ... ADD VERSION ... FROM (...)` pair. It ships in `snowflake-ml-python`, a separate package from `snowpark-python` — the two can be on different version cadences, so check both when something doesn't match the docs.
- **Client-side encrypted stages not supported** — AI_EXTRACT and fine-tuning datasets
- **Mistral not available for AI_EXTRACT** — arctic-extract is the only model. Mistral/Pixtral available for AI_COMPLETE only.
- **AI_PARSE_DOCUMENT is not a pre-step for AI_EXTRACT** — AI_EXTRACT reads files directly
- **No context window limit for AI_AGG / AI_SUMMARIZE_AGG** — the only functions that handle arbitrarily large aggregations natively
