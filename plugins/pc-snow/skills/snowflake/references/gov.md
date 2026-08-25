# Snowflake Governance

Covers: data classification, tags, masking, row access policies, lineage, auditing, GDPR.
For a policy/tag syntax you're unsure of, query `context7` first (`libraryId: /websites/snowflake_en`)
rather than trusting training data.

---

## Architecture — two layers, deliberately separate

```
Classification layer (Snowflake writes)     Enforcement layer (you decide)
────────────────────────────────────────    ──────────────────────────────
SNOWFLAKE.CORE.SEMANTIC_CATEGORY            Custom tags (MY_DB.GOV.PII_EMAIL)
SNOWFLAKE.CORE.PRIVACY_CATEGORY               └── Masking policy attached to tag
                                              └── Row access policy attached to tag
        ↓ mapping (explicit step)
        Your custom tags mirror the semantics
        but you control the policy attachment
```

**Why they're separate**: system tags are Snowflake's classification output — a read-only verdict. Masking and row access policies are your organization's enforcement decisions. Keeping them decoupled means:
- Snowflake can re-run or improve classification (new EU/India categories, Jan 2026) without touching your policies
- You can apply different policies to the same data type across contexts
- You can be more granular than Snowflake's categories (e.g. `PII_EMAIL_CUSTOMER` vs `PII_EMAIL_EMPLOYEE`)

**The limitation**: you cannot attach masking or row access policies directly to Snowflake system tags (`SNOWFLAKE.CORE.*`). They are system-managed, read-only from a policy perspective. This is by design — see above.

**pinky solution**: create custom tags that mirror Snowflake's semantic category vocabulary, then attach policies to your custom tags. The mapping from system tag value → custom tag application is the governance decision step.

---

## 1. Data classification

### Native classification functions

```sql
-- Classify a single table (returns category per column)
SELECT * FROM TABLE(
    SNOWFLAKE.DATA_PRIVACY.CLASSIFY_SENSITIVE_DATA(
        'MY_DB.MY_SCHEMA.MY_TABLE',
        BUILD_SCOPED_FILE_URL(@stage, 'config.json')  -- optional config
    )
);

-- Extract semantic categories from column values (sampling-based)
SELECT SYSTEM$EXTRACT_SEMANTIC_CATEGORIES('MY_DB.MY_SCHEMA.MY_TABLE');
```

**Output**: per-column JSON with `semantic_category` (EMAIL, PHONE, NAME, SSN, etc.) and `privacy_category` (IDENTIFIER, QUASI-IDENTIFIER, SENSITIVE).

### Automatic classification — scheduled

Classification can be run automatically on a schedule via `ALTER TABLE SET CLASSIFICATION_PROFILE`. Snowflake writes system tags directly.

```sql
-- Set a classification profile on a table
ALTER TABLE my_table SET CLASSIFICATION_PROFILE = 'DEFAULT';
```

### Available semantic categories — EU/India (since 2026-01-22)

Five new categories for automatic GDPR-relevant PII detection: `DRIVERS_LICENSE`, `NATIONAL_IDENTIFIER`, `PASSPORT`, `PAYMENT_CARD`, `TAX_IDENTIFIER`. Detected automatically — no config change required.

Full category list: `SNOWFLAKE.DATA_PRIVACY.SUPPORTED_CLASSIFICATION_CATEGORIES` view.

### Workflow: classification → custom tags

```
1. CLASSIFY_SENSITIVE_DATA          →  Snowflake system tags written
2. Read SNOWFLAKE.CORE tags         →  Identify columns that need enforcement
3. Apply custom tags                →  Explicit governance decision per column/table
4. Policies follow automatically    →  Via tag-based masking/row access
```

Step 3 can be automated in a Snowpark SP scheduled via Task — read the system tag values, apply the corresponding custom tags based on your mapping table.

---

## 2. Tags

### Custom tags — pinky convention

Tags mirror Snowflake's semantic category vocabulary. Stored in a dedicated governance schema.

```sql
-- One tag per PII category — matches Snowflake's SEMANTIC_CATEGORY values
CREATE TAG my_db.governance.pii_email       ALLOWED_VALUES 'true';
CREATE TAG my_db.governance.pii_name        ALLOWED_VALUES 'true';
CREATE TAG my_db.governance.pii_phone       ALLOWED_VALUES 'true';
CREATE TAG my_db.governance.pii_ssn         ALLOWED_VALUES 'true';
CREATE TAG my_db.governance.pii_passport    ALLOWED_VALUES 'true';
CREATE TAG my_db.governance.pii_payment     ALLOWED_VALUES 'true';

-- Apply to column
ALTER TABLE my_schema.customers
    ALTER COLUMN email SET TAG my_db.governance.pii_email = 'true';

-- Apply to table (schema-level object tags)
ALTER TABLE my_schema.customers
    SET TAG my_db.governance.data_domain = 'CRM';
```

### COPY TAGS — preserve tags on table replace (GA since 2026-04-02)

`CREATE OR REPLACE TABLE` with `COPY TAGS` preserves the replaced table's tags on the new table. Same pattern as `COPY GRANTS`.

```sql
-- Preserve tags and grants when replacing a table
CREATE OR REPLACE TABLE my_schema.my_table
    COPY GRANTS
    COPY TAGS
    AS SELECT ...;
```

**snowflake-provider impact**: add `COPY TAGS` to all `CREATE OR REPLACE TABLE` DDL generation alongside `COPY GRANTS`. Without it, a table redeploy strips all governance metadata silently.

Tag precedence with LIKE/CLONE: replaced table tags take precedence over source tags on conflict.

### Tag coverage visibility — Snowsight (Preview since 2026-04-30)

Snowsight → Governance & security → Tags & policies now shows:
- Tag creation UI (+ Create tag)
- Coverage dashboard — tagging adoption across object types in the account

**Still Preview — SQL is authoritative for IaC.** Use the Snowsight dashboard for audit/monitoring only; snowflake-provider remains the deployment path.

### Querying tag assignments

```sql
-- All columns tagged with a specific tag
SELECT *
FROM TABLE(my_db.information_schema.tag_references_all_columns(
    'MY_DB.MY_SCHEMA.MY_TABLE', 'table'
));

-- Account-wide tag assignments (Account Usage — up to 3h lag)
SELECT object_name, column_name, tag_name, tag_value
FROM snowflake.account_usage.tag_references
WHERE tag_name = 'PII_EMAIL'
ORDER BY object_name;
```

---

## 3. Masking policies

### Tag-based masking — the scalable pattern

Attach a masking policy to a custom tag once. Any column tagged automatically inherits the policy.

```sql
-- Create masking policy
CREATE MASKING POLICY my_db.governance.mask_email
    AS (val STRING) RETURNS STRING ->
        CASE
            WHEN CURRENT_ROLE() IN ('DATA_ANALYST', 'DEVELOPER') THEN val
            ELSE REGEXP_REPLACE(val, '.+@', '*****@')
        END;

-- Attach to custom tag (not to Snowflake system tags — see architecture section)
ALTER TAG my_db.governance.pii_email
    SET MASKING POLICY my_db.governance.mask_email;

-- Now any column tagged pii_email = 'true' is automatically masked
-- No per-column policy attachment needed
```

### Conditional masking — database role aware

```sql
-- Mask based on database role (preferred over account role for data access)
CREATE MASKING POLICY my_db.governance.mask_email_dbrole
    AS (val STRING) RETURNS STRING ->
        CASE
            WHEN IS_DATABASE_ROLE_IN_SESSION('MY_DB.CUSTOMERS_USER') THEN val
            ELSE REGEXP_REPLACE(val, '.+@', '*****@')
        END;
```

Using `IS_DATABASE_ROLE_IN_SESSION()` instead of `CURRENT_ROLE()` aligns with the pinky RBAC model (database roles = data access, account roles = identity).

### Masking policy visibility

```sql
-- Which policies are attached to which columns
SELECT *
FROM snowflake.account_usage.policy_references
WHERE policy_kind = 'MASKING_POLICY';
```

### snowflake-provider deployment

Masking policies and tag-policy associations are declared as objects in `snowflake-provider` manifests. The deployment order matters:
1. Create tags
2. Create masking policies
3. Associate policies to tags (`ALTER TAG ... SET MASKING POLICY`)
4. Apply tags to columns (triggers masking automatically)

---

## 4. Row access policies

Control which rows a user can see — transparent to the query (returns empty rows, not an error).

```sql
-- Row access policy: users see only their own tenant's data
CREATE ROW ACCESS POLICY my_db.governance.tenant_isolation
    AS (tenant_id VARCHAR) RETURNS BOOLEAN ->
        tenant_id = CURRENT_USER()
        OR CURRENT_ROLE() = 'ADMIN';

-- Attach to table
ALTER TABLE my_schema.orders
    ADD ROW ACCESS POLICY my_db.governance.tenant_isolation ON (tenant_id);
```

**Key difference from masking**: masking shows the row but hides/transforms the value. Row access policy hides the row entirely.

**pinky use case**: multi-tenant SiS apps where teams should only see their own data. Combined with Restricted Caller's Rights (SiS Container Runtime — preview since 2026-02-27), the viewer's rights determine row visibility.

---

## 5. Lineage

### Native lineage — what Snowflake tracks automatically

Column-level lineage for tables, views, dynamic tables — visible in Snowsight (Data → Lineage).

```sql
-- Query lineage programmatically
SELECT *
FROM snowflake.account_usage.access_history
WHERE query_start_time >= DATEADD('day', -7, CURRENT_TIMESTAMP)
  AND array_size(objects_modified) > 0
LIMIT 100;
```

### Process lineage — SPs and Tasks (GA since 2025-10-03)

The lineage graph now covers stored procedures and tasks — not just tables. Directly visible in Snowsight → Data → Lineage. Value for SP execution chains: traceable end-to-end.

### External lineage — OpenLineage REST (Preview since 2026-01-16)

External tools can push OpenLineage-compatible events to Snowflake via REST endpoint. Snowflake ingests them into the native lineage graph.

```
External tool (dbt, Airflow, custom)
    → OpenLineage event (JSON)
    → Snowflake REST endpoint
    → Native lineage graph (visible in Snowsight)
```

**pinky relevance**: if another project uses Airflow for cross-platform orchestration, Airflow can push lineage events to Snowflake to maintain a unified lineage graph. Still Preview — assess integration surface against snowflake-provider and pinky-connect when GA.

---

## 6. Auditing

### ACCESS_HISTORY — who accessed what

```sql
-- Direct column access by user (last 7 days)
SELECT
    query_start_time,
    user_name,
    query_type,
    base_objects_accessed
FROM snowflake.account_usage.access_history
WHERE query_start_time >= DATEADD('day', -7, CURRENT_TIMESTAMP)
  AND array_size(base_objects_accessed) > 0
ORDER BY query_start_time DESC;
```

`ACCESS_HISTORY` tracks which columns were actually read — not just which tables were queried. This is the right table for GDPR data access audits.

### LOGIN_HISTORY — authentication events

```sql
-- Failed logins (security monitoring)
SELECT event_timestamp, user_name, client_ip, error_message
FROM snowflake.account_usage.login_history
WHERE is_success = 'NO'
  AND event_timestamp >= DATEADD('day', -7, CURRENT_TIMESTAMP)
ORDER BY event_timestamp DESC;
```

### QUERY_HISTORY — full query audit trail

```sql
-- Queries that touched sensitive tables
SELECT query_text, user_name, role_name, start_time, execution_status
FROM snowflake.account_usage.query_history
WHERE query_text ILIKE '%customers%'
  AND start_time >= DATEADD('day', -7, CURRENT_TIMESTAMP)
ORDER BY start_time DESC;
```

**Account Usage latency**: up to 3 hours for most views (ACCESS_HISTORY up to 3h, LOGIN_HISTORY ~2h). Not suitable for real-time alerting — use `INFORMATION_SCHEMA` views for near-real-time.

---

## 7. GDPR

### What Snowflake covers natively

| Requirement | Native mechanism |
|---|---|
| PII identification | `CLASSIFY_SENSITIVE_DATA` + `EXTRACT_SEMANTIC_CATEGORIES` |
| Access control | Masking policies + row access policies (tag-based) |
| Access audit trail | `ACCESS_HISTORY` — column-level |
| Retention limits | `DATA_RETENTION_TIME_IN_DAYS` + lifecycle policies |
| Right to be forgotten | `DELETE` — but see fail-safe caveat below |
| Data classification tags | Custom tags mirroring Snowflake semantics |

### What remains a process (not automated by Snowflake)

- Mapping classification results to custom tags — needs explicit human/SP-driven step
- RTBF request tracking — Snowflake doesn't know which rows belong to a subject; your pipeline must map `subject_id → rows`
- Legal retention minimum enforcement — Snowflake can expire data, it cannot enforce a minimum retention floor; that's a process constraint

### Right to be forgotten — fail-safe caveat

`DELETE` on a permanent table does not immediately erase data. Fail-safe (7 days, Snowflake support access only) persists after time travel expires.

Full timeline after `DELETE`:
```
DELETE executed
    → Data in time travel for DATA_RETENTION_TIME_IN_DAYS (0–90 days)
    → Data in fail-safe for 7 days after time travel ends
    → Data fully gone
```

**Minimum exposure window**: even with `DATA_RETENTION_TIME_IN_DAYS = 0`, fail-safe adds 7 days.

For PII tables: set `DATA_RETENTION_TIME_IN_DAYS = 0` and document the 7-day fail-safe window for your DPO. For maximum control: use transient tables for staging PII (fail-safe = 0, time travel 0–1 days).

See `references/costs.md` for retention period guidance by data type and full lifecycle policy setup.

---

## Gotchas

- **Cannot attach policies to system tags** — `SNOWFLAKE.CORE.SEMANTIC_CATEGORY` is read-only from a policy perspective. Always use custom tags for enforcement. This is by design: classification and enforcement are deliberately decoupled.
- **COPY TAGS required on CREATE OR REPLACE TABLE** — without it, a table redeploy silently strips all tag assignments. Track alongside COPY GRANTS in snowflake-provider DDL.
- **Tag-based masking inheritance** — masking applies at query time based on the active tag-policy association. If you change the policy on a tag, all tagged columns immediately inherit the new policy — intentional, but test before pushing to production.
- **ACCESS_HISTORY = column-level, QUERY_HISTORY = query text** — for GDPR audits, `ACCESS_HISTORY` is the right source (tracks actual column reads, not just SQL text). `QUERY_HISTORY` shows intent, not access.
- **Snowsight Tags UI is Preview** — useful for monitoring, not for IaC. Don't create governance objects via the UI if snowflake-provider is the source of truth.
- **Row access policy = silent** — a blocked row returns nothing, not an error. A query that "returns 0 rows" may be a policy effect, not an empty table.
- **IS_DATABASE_ROLE_IN_SESSION vs CURRENT_ROLE** — prefer the former in masking/row access policies. Account role can change with `USE ROLE`; database role is tied to the session's database context and aligns with the pinky RBAC model.
