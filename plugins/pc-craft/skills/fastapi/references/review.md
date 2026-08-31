# FastAPI — review checklist

Applied on top of `pc-core:review-guidelines` when the code under review imports `fastapi`. Severity
labels are the same set.

## Dependency injection

- **🔴 blocking** — business logic inline in a route handler instead of behind `Depends`. The route
  wires dependencies and returns; the work lives in an injected service.
- **🔴 blocking** — a `yield` dependency that acquires a resource without releasing it on the error
  path (no `try/finally` or context manager around the `yield`).
- **🟡 important** — a singleton (HTTP client, connection pool) created per request instead of once
  in `lifespan`.
- **🟡 important** — a dependency declared as `def` where it does I/O; prefer `async def`, and use
  the `Annotated[T, Depends(...)]` form over the default-value form.

## Validation

- **🔴 blocking** — an ORM model used directly as `response_model`. Input and output Pydantic models
  are separate types.
- **🟡 important** — one schema serving both create and update. Update is partial — its own schema
  with all fields `Optional`.
- **🟡 important** — a constraint (`gt`, `le`, `EmailStr`, `max_length`) enforced in handler code
  instead of on the Pydantic field, i.e. after the boundary instead of at it.

## Async correctness

- **🔴 blocking** — a blocking call (`requests`, `time.sleep`, a sync DB driver) inside `async def`.
- **🔴 blocking** — `asyncio.run()` called inside an already-running event loop.
- **🟡 important** — `run_in_threadpool` used as the default path rather than a last resort for a
  genuinely sync-only library.

## Security

- **🔴 blocking** — `Depends(get_current_user)` treated as authorization. It proves identity only;
  every resource access still needs an explicit ownership/role check.
- **🔴 blocking** — user input f-string-interpolated into SQL.
- **🔴 blocking** — `allow_origins=["*"]` together with `allow_credentials=True` in the CORS config
  (invalid per the CORS spec and unsafe).

## Database

- **🔴 blocking** — a module-level shared session. One request-scoped session via a dependency.
- **🟡 important** — a relationship accessed inside a loop without eager loading
  (`selectinload`/`joinedload`) — the N+1 query pattern.
- **🟡 important** — a list endpoint with no pagination, or pagination whose `limit` is not capped.
