---
name: snowflake
description: Snowflake platform reference — Python Core API (snowflake.core) for IaC deployment, warehouse/compute-pool sizing and caching, PyPI packages in UDFs/procedures via the artifact repository, governance (classification, tags, masking, row access, lineage, GDPR), telemetry/event tables, cost model and tracking, and the Native App framework. Use when the work touches Snowflake deployment, sizing, governance, telemetry, cost or Native Apps.
user-invocable: false
metadata:
  source: Snowflake official docs (docs.snowflake.com) — distilled/rewritten content
  updated: 2026-08-23
---

# Snowflake — Platform

Covers the operational/IaC side of Snowflake: how to deploy objects, size and pay for compute,
package code, govern data, observe pipelines, and ship a Native App.

Stack: Snowpark Python (stored procedures, serverless tasks) + Snowflake Python Core (deployment).

> Snowflake ships fast — any class, method, parameter, limit, or preview-status claim that seems
> uncertain should be checked against `context7` (`libraryId: /websites/snowflake_en`) before being
> taken at face value. Prefer the native Python API (`snowflake.core`, `snowflake.snowpark`) over
> raw `session.sql()` wherever a Python equivalent exists — see `references/core-api.md`. Where SQL
> is shown for DDL or config that has no clean Python equivalent, that's deliberate, not an oversight.

## References

- `references/bootstrap.md` — account bootstrap: what a fresh account ships with, the one-time `ACCOUNTADMIN` setup that can't be automated, the domain-owner role-switching pattern, account roles vs database roles.
- `references/core-api.md` — `snowflake.core` (Root, the Model/Collection/Resource pattern, CreateMode, Task, Dynamic Table, Table, Alert, Schema/Database, Clone, async PollingOperation), drop-strategy design, gotchas.
- `references/warehouses.md` — warehouse types and rates, serverless Tasks billing, multi-cluster scaling policy, Streamlit cache strategy (`st.session_state` / `st.cache_resource` / `st.cache_data`), Compute Pool sizing and lifecycle, consumption queries.
- `references/artifact-repository.md` — making a PyPI package (including your own) available to a Python UDF/SP: Anaconda channel vs artifact repository vs stage upload, extras, packages policy.
- `references/gov.md` — data classification, custom tags vs system tags, tag-based masking and row access policies, lineage, auditing (ACCESS_HISTORY/LOGIN_HISTORY/QUERY_HISTORY), GDPR mechanics.
- `references/telemetry.md` — LOG_LEVEL/TRACE_LEVEL/METRIC_LEVEL, the event table, custom telemetry via `snowflake.telemetry` (`add_event`, `set_span_attribute`), storage lifecycle policy, per-schema event table architecture.
- `references/costs.md` — credit consumption model per stack component (SPs, serverless Tasks, Dynamic Tables, Streamlit, Cortex, Openflow), storage & retention costs, Resource Monitors vs Budgets vs per-user Quotas, optimization patterns.
- `references/native-app.md` — Native App Framework: provider/consumer model, `snowflake.yml`/`manifest.yml`, `setup.sql` rules, telemetry cascade, `snow app` CLI, RBAC via application roles, lifecycle (install/upgrade/uninstall), publication (private listing vs Marketplace).
- `references/review.md` — the Snowflake-stack review checklist (deployment, SP/UDF, telemetry/event table, governance), applied on top of `pc-core:review-guidelines` when reviewing code that deploys to or runs on Snowflake.

Load whichever reference fits the task — most questions need only one; a deployment question
spanning cost and governance may need two.
