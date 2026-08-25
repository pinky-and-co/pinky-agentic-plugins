# Snowflake Native App Framework

Reference: Snowflake Native App Framework (GA) — Snowflake CLI (`snow app` commands),
`snowflake.yml` definition v2. Generic provider/consumer reference: how to package, install,
upgrade and publish an application, and how to keep a single business codebase running in both
SaaS mode (your own account) and Native App mode (a consumer account).
For a CLI flag or manifest key you're unsure of, query `context7` first
(`libraryId: /websites/snowflake_en`) rather than trusting training data.

> **⚠ Active development area — track releases closely.**
> The CLI project definition is at v2 — v1 is deprecated. The simplified Native App
> (declarative sharing) is in active Preview. Apps with containers (SPCS) add a large surface.
> Re-check on every major CLI release.

---

## Mental model — provider / consumer

A Native App is distributed by a **provider** and installed into a **consumer account**. The
provider ships an *Application Package* (manifest + setup script + staged artifacts); the
consumer installs it as an *Application* that owns a database in their own account.

```
Provider account
├── Application Package  MY_APP_PKG
│   ├── manifest.yml                ← config, versions, privileges, telemetry
│   ├── setup.sql                   ← DDL run at install/upgrade in the consumer account
│   ├── shared_content_schema/      ← provider data exposed via REFERENCE_USAGE (if any)
│   └── Stage app_src.stage/        ← artifacts (Python, Streamlit, libraries…)
│       ├── libraries/
│       └── streamlit/
│
└── Private listing / Snowflake Marketplace
        ↓ consumer installs
Consumer account
└── Application MY_APP
    └── MY_APP database (PUBLIC + the schemas setup.sql creates)
```

**Invariant** — provider data never moves into the consumer account. The consumer account is a
black box: the provider's only visibility is the telemetry cascade (see Telemetry).

**Identity** — the app doesn't manage its own authentication. Consumers access it through their
own Snowflake session (Snowflake-native SSO), and Snowflake users already exist in the consumer
account before install — the app never creates them.

---

## `snowflake.yml` — project definition (v2)

```yaml
# snowflake.yml
definition_version: 2
entities:
  pkg:
    type: application package
    identifier: MY_APP_PKG
    stage: app_src.stage
    manifest: app/manifest.yml
    artifacts:
      - src: app/setup.sql
        dest: ./
      - src: app/manifest.yml
        dest: ./
      - src: libraries/
        dest: libraries/
      - src: streamlit/my_app/
        dest: streamlit/my_app/
    meta:
      role: DEPLOYER
      warehouse: DEPLOY_WH
      post_deploy:
        - sql_script: scripts/shared_content.sql   # only if the app exposes provider data

  app:
    type: application
    identifier: MY_APP
    from:
      target: pkg
    debug: true                                     # dev mode — stage, not version/patch
    telemetry:
      share_mandatory_events: true                  # cascade telemetry → provider
      optional_shared_events:
        - DEBUG_LOGS
    meta:
      role: DEPLOYER
      warehouse: DEPLOY_WH
```

```yaml
# snowflake.local.yml  ← in .gitignore (dev identifiers: role, warehouse, dev app name)
entities:
  pkg:
    identifier: MY_APP_PKG_DEV
    meta:
      role: DEVELOPER
  app:
    identifier: MY_APP_DEV
    debug: true
    meta:
      role: DEVELOPER
```

`snowflake.local.yml` is **never committed** — Snowflake templates already `.gitignore` it.

---

## `manifest.yml`

```yaml
manifest_version: 2          # mandatory — enables automated privilege grants

version:
  name: v1
  label: "My App v1"
  comment: "Distribution build — MY_APP"

artifacts:
  readme: README.md
  setup_script: setup.sql
  default_streamlit: app_public.my_app          # entry-point Streamlit

configuration:
  log_level: WARN            # no flood in production
  trace_level: ON_EVENT      # each SP that traces produces a span
  telemetry_event_definitions:
    - type: ERRORS_AND_WARNINGS
      sharing: MANDATORY
    - type: DEBUG_LOGS
      sharing: OPTIONAL

# Privileges requested from the consumer at install — consented once; upgrades reuse them.
privileges:
  - CREATE SCHEMA:
      description: "The app provisions one schema per tenant/department"
  - EXECUTE TASK:
      description: "The app runs serverless pipelines as tasks"

# References — consumer-owned objects the app needs (bound at install or via the admin UI).
references:
  - source_table:
      label: "Primary source table"
      description: "Table to process — SELECT required"
      privileges:
        - SELECT
      object_type: Table
      multi_valued: false
      register_callback: app_public.update_reference
```

`manifest_version: 2` activates automated privilege grants: the consumer sees the privilege
list at install and consents once; upgrades apply the same privileges without re-consent.

---

## `setup.sql` — canonical install/upgrade script

Runs at **every** install and upgrade. Must be idempotent. No `USE` statements — qualify every
object. Order matters: roles → versioned (stateless) schemas → stateful schemas → grants last.

```sql
-- ==============================================================
-- 1. APPLICATION ROLES — the RBAC interface exposed to the consumer
--    Cannot live in a versioned schema.
-- ==============================================================
CREATE APPLICATION ROLE IF NOT EXISTS app_admin;
CREATE APPLICATION ROLE IF NOT EXISTS app_user;
CREATE APPLICATION ROLE IF NOT EXISTS app_viewer;

GRANT APPLICATION ROLE app_user   TO APPLICATION ROLE app_admin;
GRANT APPLICATION ROLE app_viewer TO APPLICATION ROLE app_user;

-- ==============================================================
-- 2. VERSIONED SCHEMAS — stateless objects (SPs, UDFs, views)
--    Use CREATE OR ALTER VERSIONED SCHEMA — never CREATE OR REPLACE SCHEMA.
-- ==============================================================
CREATE OR ALTER VERSIONED SCHEMA app_public;

-- Reference callback — wires consumer-owned objects declared in manifest references.
CREATE OR REPLACE PROCEDURE app_public.update_reference(
    ref_name STRING, operation STRING, ref_or_alias STRING
)
  RETURNS STRING LANGUAGE SQL AS $$
BEGIN
  CASE (operation)
    WHEN 'ADD'    THEN SELECT SYSTEM$SET_REFERENCE(:ref_name, :ref_or_alias);
    WHEN 'REMOVE' THEN SELECT SYSTEM$REMOVE_REFERENCE(:ref_name, :ref_or_alias);
    WHEN 'CLEAR'  THEN SELECT SYSTEM$REMOVE_ALL_REFERENCES();
  END CASE;
  RETURN 'Success';
END;
$$;

-- Business SP — single source file, same code SaaS and Native App.
CREATE OR REPLACE PROCEDURE app_public.process(args VARIANT)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python')
  IMPORTS = ('/resources/procedure/process.py')
  HANDLER = 'process.main';

-- Admin SP — provisions a tenant schema inside the application.
-- EXECUTE AS CALLER: needs the connected consumer admin's rights (CREATE SCHEMA on the app db).
CREATE OR REPLACE PROCEDURE app_public.provision_tenant(tenant_id VARCHAR)
  RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python')
  IMPORTS = ('/resources/procedure/provision_tenant.py')
  HANDLER = 'provision_tenant.main'
  EXECUTE AS CALLER;

-- Streamlit app
CREATE OR REPLACE STREAMLIT app_public.my_app
  FROM '/streamlit/my_app'
  MAIN_FILE = 'app.py';

-- ==============================================================
-- 3. STATEFUL SCHEMA — persistent state (survives upgrades → IF NOT EXISTS).
-- ==============================================================
CREATE SCHEMA IF NOT EXISTS app_state;

CREATE TABLE IF NOT EXISTS app_state.tenants (
    tenant_id      VARCHAR PRIMARY KEY,
    status         VARCHAR DEFAULT 'ACTIVE',
    provisioned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==============================================================
-- 4. GRANTS — last, after every object exists.
-- ==============================================================
GRANT USAGE  ON SCHEMA    app_public               TO APPLICATION ROLE app_viewer;
GRANT USAGE  ON SCHEMA    app_state                TO APPLICATION ROLE app_admin;
GRANT USAGE  ON STREAMLIT app_public.my_app        TO APPLICATION ROLE app_viewer;
GRANT USAGE  ON PROCEDURE app_public.process(VARIANT)            TO APPLICATION ROLE app_admin;
GRANT USAGE  ON PROCEDURE app_public.provision_tenant(VARCHAR)   TO APPLICATION ROLE app_admin;
GRANT SELECT ON TABLE     app_state.tenants        TO APPLICATION ROLE app_viewer;
```

### Critical rules for `setup.sql`

| Rule | Why |
|---|---|
| No `USE` statements | Forbidden in any Native App setup.sql — `CREATE SCHEMA` does not change session context |
| Qualify every object | `CREATE TABLE app_state.tenants`, not `CREATE TABLE tenants` |
| `CREATE IF NOT EXISTS` for stateful tables | State survives upgrades — never recreate it |
| `CREATE OR ALTER VERSIONED SCHEMA` | Never `CREATE OR REPLACE SCHEMA` on a versioned schema |
| `CREATE OR REPLACE` for SPs and views | Stateless code — recreating is safe |
| Grants after all objects | If the script fails between a `CREATE OR REPLACE` and its `GRANT`, consumers lose access |
| Application roles not in a versioned schema | `CREATE APPLICATION ROLE` fails inside a versioned schema |
| `EXECUTE AS CALLER` allowed for setup SPs | Supported in Native Apps (unlike standard SPs) — lets a setup SP create account-level objects with the connected admin's rights |

---

## Telemetry — provider visibility into installed apps

In Native App, the consumer account is a black box: events cascade to the provider through the framework's telemetry sharing.

```yaml
# manifest.yml
configuration:
  telemetry_event_definitions:
    - type: ERRORS_AND_WARNINGS
      sharing: MANDATORY
    - type: DEBUG_LOGS
      sharing: OPTIONAL
```

```yaml
# snowflake.yml (app entity)
app:
  telemetry:
    share_mandatory_events: true
    optional_shared_events:
      - DEBUG_LOGS
```

App SPs emit span attributes (same tracing pattern as standard SPs — see `references/telemetry.md`).
The provider reads them back via `SNOWFLAKE.TELEMETRY.APP_TELEMETRY_EVENTS_VIEW`, filtered on the
package name:

```sql
SELECT
    PARSE_JSON(record:body):tenant_id::STRING       AS tenant_id,
    resource_attributes:snow.account.name::STRING   AS consumer_account,
    MAX(timestamp)                                  AS last_seen
FROM SNOWFLAKE.TELEMETRY.APP_TELEMETRY_EVENTS_VIEW
WHERE resource_attributes:snow.application.package.name = 'MY_APP_PKG'
GROUP BY 1, 2;
```

> `APP_TELEMETRY_EVENTS_VIEW` has ~3h latency (same as `ACCOUNT_USAGE`). It is not a real-time
> signal — for near-real-time registration use an install/provisioning callback instead.

---

## Snowflake CLI — `snow app` commands

```bash
snow app init my_app                       # scaffold the project (once)
snow app run --connection="dev"            # rebuild + deploy + upgrade the test app instance
snow app deploy --connection="prod"        # deploy only the package (CI/CD — no app instance)
snow app version create V1                 # create a version (captures the stage state)
snow app version create V1 --patch 1       # add a patch to V1
snow app run --version V1 --patch 1        # test exactly what the consumer installs
snow app open                              # open the default Streamlit in the browser
snow app teardown                          # DROP the dev app instance (never in prod)
```

**Dev mode** (`debug: true`): edited Python files on the stage are picked up without re-running
`setup.sql` — fast iteration on libraries only.
**Version mode** (`--version V1`): `setup.sql` is fully re-executed — mandatory before cutting a
release, since stage dev mode does not reproduce a real install exactly.

---

## RBAC — application roles exposed to the consumer

```
app_admin   ← consumer ACCOUNTADMIN / technical team
    └── app_user
        └── app_viewer  ← read-only

-- the consumer grants application roles to their own roles:
GRANT APPLICATION ROLE MY_APP.app_viewer TO ROLE MY_ANALYST_ROLE;
GRANT APPLICATION ROLE MY_APP.app_admin  TO ROLE MY_DATA_TEAM_ROLE;
```

---

## Lifecycle — install → upgrade → uninstall

### Install

```
Consumer installs MY_APP (private listing or Marketplace)
    ↓ setup.sql runs in the consumer account
    ├── creates MY_APP database + schemas (app_public, app_state)
    ├── deploys SPs / UDFs / Streamlit
    └── tenant schemas are NOT auto-created — provisioned later via the admin UI / SP
```

### Upgrade (provider publishes a new version)

```
snow app version create V2
    ↓ setup.sql V2 runs (UPGRADE)
    ├── stateful tables (IF NOT EXISTS) → untouched, data preserved
    ├── SPs / views (OR REPLACE) → new code
    └── grants → reapplied
```

State tables created `IF NOT EXISTS` are never recreated on upgrade — tenant data survives
across versions.

### Uninstall

```
Consumer → DROP APPLICATION MY_APP
    → everything setup.sql created is removed
    → objects created by EXECUTE AS CALLER SPs (account-level roles/users) SURVIVE
      → must be cleaned up manually or via a documented "/uninstall" procedure
```

A clean uninstall flow is a prerequisite for a Marketplace listing — Snowflake checks it in the
security review.

---

## Versions, patches and release channels

```
MY_APP_PKG
├── V1 (stable)
│   ├── Patch 0  ← initial release
│   ├── Patch 1  ← setup.sql bug fix
│   └── Patch 2  ← new UDF
└── V2 (next)
    └── Patch 0  ← in development
```

Consumers on V1 receive V1 patches automatically (unless a maintenance policy says otherwise).
V2 needs a voluntary migration or a release directive.

**Release channels** (`enable_release_channels: true`): publish V2 to the `beta` channel first,
validate on a few consumers, then promote to `stable`. Worth it for apps distributed to
regression-sensitive enterprise consumers.

---

## Publication — in-account private listing vs Marketplace

Two ways to expose an Application Package to consumers:

| Mode | What it is | When to use |
|---|---|---|
| **In-account / private listing** | The package is shared directly to specific accounts (or organisation), off the public catalogue. The consumer installs from a private listing they were granted. | Targeted distribution: a named enterprise consumer, an internal cross-account deployment, or beta validation before going public. No Snowflake marketplace review. |
| **Snowflake Marketplace** | A public listing in the catalogue, discoverable by any account. Billing flows through the Marketplace. | Broad/commercial distribution. Requires passing Snowflake's listing security review (incl. a clean uninstall flow) and a provider profile. |

Both install the same package and run the same `setup.sql`. The difference is discovery and
billing surface, not the application itself — start on a private listing, promote to Marketplace
once the uninstall flow and security review are ready.

---

## Gotchas

- **`USE` forbidden in `setup.sql`** — qualify every object; `CREATE SCHEMA` does not change session context.
- **Stateful tables never recreated** — they are `IF NOT EXISTS`; a `CREATE OR REPLACE` would destroy consumer data.
- **Application roles not in versioned schemas** — `CREATE APPLICATION ROLE` lives in the application context (step 1 of setup.sql).
- **`EXECUTE AS CALLER` allowed** in Native App SPs — the mechanism that lets a setup SP create account-level objects with the connected admin's rights. Account-level objects it creates are **not** removed by `DROP APPLICATION`.
- **Telemetry latency ~3h** — `APP_TELEMETRY_EVENTS_VIEW` is not real-time; use an install/provisioning callback for near-real-time signals.
- **Standard vs Enterprise edition** — `ALTER SCHEMA SET EVENT TABLE` is unavailable on Standard; plan an application-level event table fallback.
- **Version mode before release** — always test `snow app run --version V1` before `snow app version create`; stage dev mode (`debug: true`) does not reproduce a real install exactly.
