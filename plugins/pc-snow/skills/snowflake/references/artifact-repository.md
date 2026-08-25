# PyPI packages in Python UDFs and stored procedures

> How to make a Python package — including one you publish yourself — available to a Snowflake UDF
> or stored procedure, and which keyword selects which source. Out of scope: building or publishing
> the library. This file picks up once it is on PyPI. For the Python-managed deployment path
> (creating the `Function`/`StoredProcedure` object itself), see `references/core-api.md` —
> `ARTIFACT_REPOSITORY`/`PACKAGES` are properties of that object, not a separate mechanism.

---

## The three access paths

The same keyword means different things depending on whether `ARTIFACT_REPOSITORY` sits next to it.
That is the distinction to hold on to:

| Declaration | Where the package comes from | Typical use |
|---|---|---|
| `PACKAGES = (...)` **alone** | Snowflake's **curated Anaconda channel** | `requests`, `pandas`, `lxml` — the common third-party set |
| `ARTIFACT_REPOSITORY = ...` **+** `PACKAGES = (...)` | **PyPI**, via the artifact repository | anything on PyPI, including packages you publish yourself |
| `IMPORTS = ('@stage/...')` | a **Snowflake stage** | a wheel or source file uploaded by hand |

They combine freely: a UDF can pull its own package from PyPI and a stage-uploaded asset at once.

To check what the Anaconda channel carries:

```sql
SELECT * FROM SNOWFLAKE.INFORMATION_SCHEMA.PACKAGES
WHERE PACKAGE_NAME = 'requests' AND LANGUAGE = 'python';
```

---

## Canonical form — your own package, straight from PyPI

```sql
CREATE OR REPLACE FUNCTION UDF_VALIDATE_SIRET("VALUE" VARCHAR)
RETURNS OBJECT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.12'
ARTIFACT_REPOSITORY = snowflake.snowpark.pypi_shared_repository
PACKAGES = ('pinky-core[validate]')
HANDLER = 'run'
AS $$
from pinky_core.validate import validate_siret

def run(value):
    return validate_siret(value).model_dump()
$$;
```

Prerequisite, once per account:

```sql
GRANT DATABASE ROLE SNOWFLAKE.PYPI_REPOSITORY_USER TO ROLE <target_role>;
```

**A package you publish is reachable this way** — it needs neither inclusion in the Anaconda channel
nor staging. This is the default path; stage upload is the fallback below, not the norm.

The resolved version is **snapshotted when the object is created**, so later invocations are
deterministic. Upgrading means recreating the UDF or procedure.

---

## Extras are never implicit — the failure that looks like a platform bug

`PACKAGES = ('pinky-core')` installs the **base** dependencies only. Anything behind a PEP 508 extra
must be named: `PACKAGES = ('pinky-core[validate]')`.

Omitting it **compiles fine** and fails at call time:

```
Python Interpreter Error: ModuleNotFoundError: No module named 'stdnum'
  in function UDF_VALIDATE_SIRET with handler run
```

This reads like a dependency-resolution failure on Snowflake's side. It is not — an extra is opt-in
by definition, and the resolver did exactly what it was asked. Check the package's
`[project.optional-dependencies]` before blaming the platform. Extras syntax inside `PACKAGES` is
supported (validated live, 2026-08-04).

Version specifiers work too (`'pkg>=1.2'`). To preview what a spec resolves to:

```sql
SELECT SYSTEM$RESOLVE_PYTHON_PACKAGES('3.12', $$('numpy>=1.20.0', 'pandas==1.3.0')$$);
```

---

## Fallback — stage upload

Still valid, and the only option when the artifact repository is unavailable on the account, or when
the package is published nowhere.

```bash
pip wheel <package> --no-deps -w dist/      # no-deps: do not re-bundle dependencies
```

```sql
PUT file://dist/pinky_connect-0.1.0-py3-none-any.whl @MY_SCHEMA.PACKAGES_STAGE/;

CREATE OR REPLACE FUNCTION MY_SCHEMA.CONCURRENT_HTTP(ARGS VARIANT)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.12'
IMPORTS = ('@MY_SCHEMA.PACKAGES_STAGE/pinky_connect-0.1.0-py3-none-any.whl')
PACKAGES = ('requests', 'lxml')     -- its dependencies, from the Anaconda channel
EXTERNAL_ACCESS_INTEGRATIONS = (MY_EAI)
SECRETS = ('MY_SECRET' = MY_SECRET_OBJ)
HANDLER = 'pinky_connect.udf.concurrent_http';
```

`IMPORTS` for the custom wheel, `PACKAGES` for its dependencies. Note that you resolve the dependency
tree yourself here — exactly the work the artifact repository removes.

---

## Packages policy — governance, not access

`CREATE PACKAGES POLICY` allowlists or blocks packages account-wide, and can add creation endpoints:

```sql
CREATE PACKAGES POLICY allow_internal
  LANGUAGE PYTHON
  ALLOWLIST = ('pinky-core', 'pinky-connect')
  ADDITIONAL_CREATION_ENDPOINTS = ('https://pypi.org/simple');

ALTER ACCOUNT SET PACKAGES POLICY allow_internal;
```

It is a **governance** control — what developers may install — not the mechanism that grants access
to PyPI. Reach for it to constrain, not to make your own package resolvable.

---

## Rule for generated DDL

Generated deployment DDL uses the **artifact repository path by default**:
`ARTIFACT_REPOSITORY` + `PACKAGES`, with the extra named when the handler needs one. No wheel to
build, stage or version.

Stage upload (`IMPORTS`) stays available for accounts without the artifact repository, and for
packages that are not published — it is a fallback, and generated DDL should say so when it emits it.

---

## Correction note — 2026-08-04

Two claims in this file were wrong, and both were load-bearing:

1. **"`pypi_shared_repository` is a Snowflake-curated repo, so your packages will never be in it
   automatically."** False. A package published on PyPI resolves through `ARTIFACT_REPOSITORY` +
   `PACKAGES` with nothing but the database-role grant above. The claim conflated the Anaconda
   channel (curated) with the artifact repository (PyPI).
2. **`ARTIFACT_REPOSITORY_PACKAGES`** was listed as a legitimate third keyword. **It does not
   exist** — Snowflake answers `SQL compilation error: invalid property 'ARTIFACT_PACKAGES' for
   'FUNCTION'`. It came from a May 2025 blog article, captured verbatim in an internal reference
   folder, then copied into design docs and knowledge agents. At each hop it gained authority and
   lost its "unverified third-party source" tag, until an agent recommended it by citing another
   internal doc that had it from the same article.

Both were corrected after live validation on a trial account. The lesson is the one this file's own
header already carried and nobody applied: on a recent or preview surface, check the official docs
**before** writing the convention down.
