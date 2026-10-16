---
name: cortex
description: Snowflake Cortex — AI SQL/Snowpark functions (AI_EXTRACT, AI_COMPLETE, AI_CLASSIFY, AI_FILTER, guardrails, arctic-extract fine-tuning) and the managed Cortex Search service (hybrid vector + lexical RAG). Use for document extraction, classification, LLM guardrails, or semantic search/RAG on Snowflake.
user-invocable: false
metadata:
  source: Snowflake official docs (docs.snowflake.com) — distilled/rewritten content
  updated: 2026-08-22
---

# Cortex — Snowflake AI

Two capability families: AI SQL/Snowpark functions (extraction, classification, filtering, summarization, embeddings, completion) and the managed Cortex Search service (hybrid vector + lexical RAG).

Stack: Snowpark Python (stored procedures, serverless tasks) + Snowflake Python Core (deployment).

> Cortex moves fast — any function signature, limit, service parameter, or available model that
> seems uncertain should be checked against `context7` (`libraryId: /websites/snowflake_en`)
> before being taken at face value. Never hardcode a perishable value (version, quota, preview
> region list) in this file or its references — use a `context7` pointer instead.

## References

- `references/cortex-ai-functions.md` — full function map, `AI_EXTRACT` (responseFormat, scores, scale_factor, arctic-extract fine-tuning), the 3 guardrail mechanisms, content-hash Cortex cache, warehouse sizing, gotchas.
- `references/cortex-search.md` — `CREATE CORTEX SEARCH SERVICE` syntax (single/multi-index), source table design, key parameters (TARGET_LAG, PRIMARY KEY, REFRESH_MODE, EMBEDDING_MODEL, AUTO_SUSPEND), querying via Snowflake Python Core, filter operators, monitoring, gotchas.

Load whichever reference fits the task: document extraction, classification, sentiment, guardrails, fine-tuning → `cortex-ai-functions.md`. Semantic/hybrid search over a corpus, RAG → `cortex-search.md`. The two combine for a full RAG pipeline (see "Typical pipelines" in `cortex-ai-functions.md`).
