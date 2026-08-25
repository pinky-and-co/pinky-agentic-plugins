# Snowflake — Account Bootstrap (RBAC pattern)

Covers what a fresh account ships with, the one-time `ACCOUNTADMIN` setup that cannot be automated,
and why that setup switches to a domain-owner role at each step instead of running everything as
`ACCOUNTADMIN`. This is the generic pattern — a specific deployment's actual role/user/warehouse
names and SQL live in that deployment's own private plugin, not here.

---

## What a fresh account ships with

| Object | Name | Notes |
|---|---|---|
| Database | `SNOWFLAKE` | System metadata — read-only |
| Role | `ACCOUNTADMIN` | Full account control — never used for day-to-day operations |
| Warehouse | `COMPUTE_WH` | Default compute |

Everything else — a CI/CD role, a service user, a network policy, a deploy warehouse, account-level
grants — must be created once by a human holding `ACCOUNTADMIN`, before any automated deployment
tool can take over.

## What can never be automated, and why

| Step | Requires | Why it can't be delegated |
|---|---|---|
| Create roles, network policies | `SECURITYADMIN` | Security domain — only a human at `ACCOUNTADMIN` can grant this role initially |
| Create a service user | `USERADMIN` | User lifecycle — same bootstrapping constraint |
| A PAT / token value | — | Shown once by Snowflake, must be copied by a human immediately |
| Account-level grants (`CREATE DATABASE ON ACCOUNT`, ...) | `ACCOUNTADMIN` | No role below `ACCOUNTADMIN` can grant `ON ACCOUNT` |
| The first deploy warehouse | `SYSADMIN` | Chicken-and-egg: the CI/CD role needs a warehouse to run, but normally creates its own warehouses |

Everything past this one-time setup — databases, schemas, pipeline warehouses, tables, tasks,
grants on objects — belongs to CI/CD, not to a human running SQL by hand.

## Why the setup switches roles instead of staying on `ACCOUNTADMIN`

`ACCOUNTADMIN` can do everything — that's exactly why the bootstrap deliberately avoids using it for
everything. Each step runs as the role that is the *domain owner* for that class of object, not just
a role that happens to have permission:

| Domain | Role used | Why this role, not `ACCOUNTADMIN` |
|---|---|---|
| Roles, network policies | `SECURITYADMIN` | Canonical owner of the security domain |
| Users | `USERADMIN` | Owns user lifecycle — distinct from security policy |
| Account-level privileges | `ACCOUNTADMIN` | No alternative — only `ACCOUNTADMIN` can grant `ON ACCOUNT` |
| Operational warehouses | `SYSADMIN` | Owns compute/data objects — warehouses aren't security objects |

**Why it matters**:
1. **Audit trail clarity** — `QUERY_HISTORY` records the active role for every statement; the role alone tells you which domain a change touched.
2. **Blast radius** — a mistake under `SECURITYADMIN` cannot touch warehouses; a mistake under `SYSADMIN` cannot touch role grants.
3. **Documents ownership** — the role switch is executable documentation, legible to any Snowflake-experienced operator without reading every grant.
4. **Operational roles report to `SYSADMIN`, not `ACCOUNTADMIN`** — day-to-day role management shouldn't require the break-glass role.

`ACCOUNTADMIN` is a break-glass role: every step doable by a more scoped role should be.

## Account roles vs database roles — the two-layer model

Most RBAC designs think only in account roles, and end up re-granting object privileges to every
role on every new database, forever. The composable model separates two questions:

| Layer | Object | Answers |
|---|---|---|
| Account roles | `ROLE` | Who are you in this account? |
| Database roles | `DATABASE ROLE` | What can you access in *this* database? |

```sql
-- Define access once, at the database level
CREATE DATABASE ROLE my_db.my_app_user;
GRANT USAGE ON DATABASE my_db          TO DATABASE ROLE my_db.my_app_user;
GRANT USAGE ON SCHEMA my_db.my_schema  TO DATABASE ROLE my_db.my_app_user;
GRANT SELECT ON ALL TABLES IN SCHEMA my_db.my_schema TO DATABASE ROLE my_db.my_app_user;

-- Grant the database role to whoever needs it
GRANT DATABASE ROLE my_db.my_app_user TO ROLE some_account_role;
```

**Why database roles compose and direct grants don't**:
- A database role lives inside the database — it moves with it on clone, share, and Native App packaging. Direct account-role grants do not.
- A new consumer is one `GRANT DATABASE ROLE` statement, not a re-grant of every object privilege.
- Data sharing and Native Apps can only expose database roles externally — account roles aren't shareable.

**Diagnostic signal of broken RBAC** — object privileges granted directly to an account role:
```sql
-- Symptom: the database role layer was skipped, RBAC won't survive a second database.
GRANT SELECT ON ALL TABLES IN SCHEMA my_schema TO ROLE some_account_role;
```

Naming convention for database roles: suffix by relationship to the domain, not by the SQL granted
underneath — `_VIEWER` (read), `_USER` (use the feature), `_CREATOR` (create objects), `_ADMIN`
(manage).

## Gotchas

- **PAT tokens expire** — set a rotation reminder before expiry; an expired PAT silently blocks every CI run.
- **`MODIFY LOG LEVEL`/`TRACE LEVEL`/`METRIC LEVEL` are not implied by ownership** — `ALTER <object> SET LOG_LEVEL` fails without the explicit account-level grant, even for the object's owner.
- **`ALTER STREAMLIT ... PULL` is not exposed by `snowflake.core`** — call it via `session.sql()` if a deployment procedure needs to refresh a Git-backed Streamlit app.
- **A CI-scoped network policy can point at a Snowflake-managed network rule** (e.g. GitHub Actions' `SNOWFLAKE.NETWORK_SECURITY.GITHUBACTIONS_GLOBAL`) — no manual IP-range maintenance as the CI provider rotates runner IPs.
- **The full list of `SNOWFLAKE.*` database roles evolves every release** — query `SHOW DATABASE ROLES IN DATABASE SNOWFLAKE` rather than trusting a fixed list; check `context7` for current governance/AI/SPCS roles before granting one you're unsure of.
