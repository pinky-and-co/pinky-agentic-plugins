# Streamlit in Snowflake

---

## Two runtimes

| | Container Runtime | Standard Runtime (legacy) |
|---|---|---|
| Streamlit version | You control it in `requirements.txt` | Controlled by Snowflake (Anaconda channel) |
| Python packages | Full pip access | Anaconda channel only |
| GPU | Yes | No |
| Sleep timer | No | Yes |
| App sharing | Viewer URLs — no Snowflake account required | Snowsight only |
| `config.toml` | Supported natively | Not supported |
| GA since | 2026-03-09 | Always available |

**Container Runtime is the modern default.** Snowflake's Snowsight wizard now defaults to it, and
it's the more capable option across the board — full pip access, GPU, no sleep timer, viewer URL
sharing, native `config.toml`. Standard Runtime is best treated as legacy for new work; its
warehouse-based constraints (Anaconda-only packages, sleep timer, no GPU) are worth knowing only
when maintaining an existing app built on it.

Pin the Container Runtime version explicitly in `requirements.txt` to avoid unexpected upgrades —
you control it, so an unpinned dependency is purely self-inflicted drift.

---

## SiS constraints vs desktop Streamlit

| Feature | SiS | Desktop |
|---|---|---|
| `st.connection("snowflake")` | Native — session-scoped, no credentials needed | Needs explicit config |
| `st.secrets` | Snowflake Secrets integration + auto env var mapping | `.streamlit/secrets.toml` |
| File system | No persistent filesystem — stage for file storage | Local filesystem |
| Background threads | Killed at session end | Persistent |
| Custom components | Supported | Supported |
| `st.html` + JavaScript | Supported (introduced 1.52.0) | Supported |
| External network calls | Requires External Access Integration | Unrestricted |
| `st.bokeh_chart` | Removed (1.52.0) — use `streamlit-bokeh` component | Removed (1.52.0) |
| Caller's rights | Session-scoped connections supported (introduced 1.53.0) | N/A |

Version numbers above mark introduction points at time of writing — verify current status via
`context7` before relying on one for a decision (see the note in `SKILL.md`).

---

## Evaluating a version upgrade

Container Runtime puts the version fully in your control via `requirements.txt` — no Anaconda
channel lag to wait out, but also no forced upgrade cadence, so someone has to decide when to move.

```
1. Feature evaluation    — does the target version add something genuinely worth the upgrade cost?
                           If not → skip
2. Local dev + test      — develop against the target version locally
3. Integration tests     — validate the CSS design system visually in Container Runtime
4. Publish               — bump the pin, tag a release
```

**Step 3 is the real gate.** Internal Streamlit CSS selectors can change between versions —
`st.tabs`, `st.expander`, `st.dataframe`, and `st.sidebar` have historically been the most
volatile. A version bump that adds a wanted feature can still silently break a custom CSS design
system layered on top of Streamlit's default styling; visual regression-check those four
components specifically before committing to a pin bump.

This reference doesn't maintain a rolling list of "what's new since version X" — Streamlit's
release cadence (roughly monthly) makes that stale within weeks. For a concrete upgrade decision,
pull the actual changelog from `context7` or Streamlit's release notes at decision time, and weigh
each candidate feature against the CSS-regression risk of the components it touches.

---

## Layout constraints — what's native vs what's a hack

### What Streamlit supports natively
- Sidebar (left) + main content area — the canonical layout
- Multi-page navigation via `st.navigation` + `st.Page`
- `st.Page(visibility="hidden")` (introduced 1.55.0) — page exists, navigable via `st.switch_page()`, hidden from sidebar nav. Use for: detail pages, sub-flows, conditional admin pages

### What requires CSS hacks — and why to avoid them

**Sidebar + topbar layout** — not natively supported. The only way to get a topbar is to hide Streamlit's default header and inject a fixed-position div via `st.html` + CSS. Tried in production: the result works visually but is brittle — every Streamlit upgrade can break the positioning, the z-index stack, or the interaction with sidebar toggle. **Avoid until Streamlit ships a native topbar API.** The maintenance cost is rarely worth the UX gain.

General rule: if achieving a layout requires overriding Streamlit's internal CSS classes (not just theming variables), it's a hack. Theming variables (`--primary-color`, `--background-color`, etc.) are stable across versions. Internal classes (`[data-testid="stSidebar"]`, `.stApp`, etc.) are not guaranteed stable.

### `st.Page(visibility=...)` — the right way to hide pages

```python
# Page exists, navigable programmatically, not shown in sidebar
st.Page("admin/settings.py", title="Settings", visibility="hidden")

# Navigate to it from another page
if user.is_admin:
    st.switch_page("admin/settings.py")
```

Before this existed, the workaround was filtering `st.navigation()` pages dynamically based on session state — which worked but was verbose. `visibility="hidden"` is cleaner and the intent is explicit.

---

## CSS design system — upgrade process

A CSS design system layered on top of Streamlit's defaults necessarily depends on internal
Streamlit CSS selectors that can change between versions.

Before any upgrade:
1. Run a visual comparison of all custom UI components against the new version
2. Check for class renames — Streamlit occasionally refactors internal CSS classes
3. Pay particular attention to: `st.tabs`, `st.expander`, `st.dataframe`, `st.sidebar` — these have the most volatile internal structure
4. Validate in Container Runtime with the target version before committing the pin

---

## SiS-specific APIs

```python
# Native Snowflake connection — no credentials in code
conn = st.connection("snowflake")
df = conn.query("SELECT * FROM my_table")

# Secrets — Snowflake Secrets Object mapped to env vars
import os

api_key = os.environ["MY_API_KEY"]  # set via st.secrets + EAI

# Caller's rights connection (Container Runtime, introduced 1.53.0)
conn = st.connection("snowflake", caller_security_mode="caller")

# Page visibility (introduced 1.55.0)
st.Page("page.py", visibility="hidden")  # programmatic nav only
```

---

## Partial reruns — `st.fragment`

Streamlit's biggest UX limitation is the full-script rerun on every interaction.
`st.fragment` (GA since 1.37.0) solves this: a decorated function reruns **only itself**
when a widget inside it changes — the rest of the page stays untouched.

### Why it matters for SiS apps

In a typical SiS Streamlit app, a full rerun means re-querying Snowflake or
re-rendering heavy dataframes even when the user just toggled an unrelated filter.
`st.fragment` isolates interactive sections so only the relevant query fires.

### Basic pattern

```python
import streamlit as st

conn = st.connection("snowflake")


# This section reruns independently when its widgets change
@st.fragment
def filter_panel():
    schema = st.selectbox("Schema", ["RAW", "CLEAN", "MART"])
    df = conn.query(
        f"SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ?",
        params=[schema],
    )
    st.dataframe(df)


# This only reruns on full page rerun, not when filter_panel widgets change
st.title("Table Explorer")
st.metric(
    "Total tables",
    conn.query("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES").iloc[0, 0],
)

filter_panel()
```

### Rules

| Rule | Detail |
|------|--------|
| Scope | Only widgets **inside** the fragment trigger a fragment rerun |
| Nesting | Fragments can be nested — inner fragment reruns don't trigger outer |
| `st.session_state` | Shared across fragments and main script — use for cross-fragment communication |
| Triggering full rerun | Call `st.rerun()` inside a fragment to force full-page rerun when needed |
| `run_every` | `@st.fragment(run_every="30s")` — auto-refresh a fragment on interval (polling pattern) |
| Caching | `@st.cache_data` still works inside fragments — combine both for best perf |

### When to use

- **Interactive filters** that control a heavy query or chart — isolate so other sections don't rerun
- **Auto-refresh panels** (monitoring, live metrics) — `run_every` polls without disrupting user interaction elsewhere
- **Multi-section dashboards** — each KPI block as a fragment, independent refresh
- **Chat/log panels** — streaming output in a fragment while the rest of the page stays stable

### When NOT to use

- **Single-widget pages** — no benefit, the whole page IS the interaction
- **Cross-widget dependencies** — if widget A must trigger a rerun of widget B's section, fragments add complexity (use session_state + `st.rerun()` pattern instead)
- **Shared expensive computation** — if multiple sections depend on the same query result, cache it at page level rather than fragmenting

### Combination with `st.cache_data`

```python
@st.cache_data(ttl=300)
def load_heavy_data():
    return conn.query("SELECT ...")


@st.fragment
def interactive_section():
    data = load_heavy_data()  # cached, not re-queried on fragment rerun
    col = st.selectbox("Column", data.columns)
    st.bar_chart(data[col])
```

### `run_every` — auto-refresh without full rerun

```python
@st.fragment(run_every="10s")
def live_metrics():
    row = conn.query(
        "SELECT COUNT(*) as cnt FROM EVENTS WHERE CREATED_AT > DATEADD('minute', -5, CURRENT_TIMESTAMP())"
    ).iloc[0]
    st.metric("Events (last 5 min)", row["cnt"])
```

The rest of the page is untouched — user can interact with filters elsewhere
while this panel auto-refreshes.

### Cache layering — and the SPCS cold-start caveat

`st.cache_data` / `st.cache_resource` / `st.session_state` are all **in-memory**: they are wiped on a
**cold start** — and in SPCS / Container Runtime a deploy or a new node in the poll system is a cold
start. So for anything **expensive or paid** (a Cortex `ai_complete`, a heavy query), an in-memory
cache alone means you pay again on every redeploy.

Layer the caches by lifetime:

| Layer | Survives | Use for |
|---|---|---|
| `st.session_state` | the session | hot, per-user state |
| `st.cache_data` / `st.cache_resource` | the running container | warm reuse within a deployment |
| **Snowflake table** (content-hash keyed) | **cold starts, deploys, nodes** | expensive/paid results that must persist |

For paid AI results the durable table layer is **mandatory**, not optional — pattern in the
`cortex` skill's `cortex-ai-functions.md` § "Caching Cortex calls".

---

## Gotchas

- **`st.bokeh_chart` removed (1.52.0)** — use `streamlit-bokeh` custom component
- **External network calls require EAI** — `requests`, `httpx`, any outbound HTTP needs an External Access Integration declared
- **No persistent filesystem** — always use Snowflake stages for file I/O in SiS
- **A server framework change can break Tornado-dependent code** — if any custom component or underlying library assumes Tornado specifically, check the current server framework via `context7` before upgrading past a version that changes it
- **Pin the version in `requirements.txt`** — Container Runtime gives you full control; don't leave it unpinned
