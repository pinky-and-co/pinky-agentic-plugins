---
name: fastapi
description: FastAPI review conventions — dependency injection, Pydantic input/output model separation, async correctness, authorization vs authentication, request-scoped DB sessions, pagination. Use when reviewing or writing a FastAPI service (routes, routers, `Depends`, `lifespan`).
user-invocable: false
metadata:
  source: original
  updated: 2026-08-31
---

# FastAPI conventions

Stack-specific conventions for a FastAPI service. Generic Python and universal quality checks are
in `pc-core:review-guidelines` — this skill is the FastAPI layer on top, invoked on demand when the
code under review imports `fastapi`.

## References

- `references/review.md` — the review checklist: DI, validation, async, security, database. Severity
  labels match `pc-core:review-guidelines` (`🔴 blocking` / `🟡 important` / `🟢 nit`).
