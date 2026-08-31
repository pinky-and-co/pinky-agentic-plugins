# Streamlit in Snowflake — review checklist

Applied on top of `pc-core:review-guidelines` when the code under review is a Streamlit app running
inside Snowflake (imports `streamlit`, runs in SiS). Severity labels are the same set.

## Blocking

- **🔴** — `st.set_page_config()` called from a page component. It belongs in `page.py` only, called
  once.
- **🔴** — a Snowflake session created directly in the app (`Session.builder.create()`). Use the
  session SiS injects (`get_active_session()`), or one passed in as a parameter.
- **🔴** — business logic inside a Streamlit callback. A callback calls a service function; it does
  not contain the logic.

## Important

- **🟡** — a data-access helper that bypasses the app's local/Snowflake transparency abstraction,
  when the app has one.
- **🟡** — CSS loaded globally in `style.css` instead of module-scoped. Each `ui_components/module/`
  owns its own `.css`; `style.css` is Streamlit normalization only.
- **🟡** — `st.cache_data` missing on a query re-run on every rerender with no user-driven change
  (static reference data, slow aggregates).
- **🟡** — a hardcoded database or schema string. Resolve it through the app's environment/naming
  convention.

## Nit

- **🟢** — `st.experimental_*` deprecated APIs where a stable equivalent exists.
