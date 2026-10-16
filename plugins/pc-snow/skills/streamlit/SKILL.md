---
name: streamlit
description: Streamlit in Snowflake (SiS) — Container vs Standard runtime, SiS-specific APIs (st.connection, secrets, caller's rights), partial reruns (st.fragment), cache layering against cold starts, layout/CSS constraints, upgrade evaluation methodology. Use for building or upgrading a Streamlit app that runs inside Snowflake.
user-invocable: false
metadata:
  source: Snowflake official docs (docs.snowflake.com) + Streamlit official docs (docs.streamlit.io, Apache-2.0) — distilled/rewritten content
  updated: 2026-08-25
---

# Streamlit in Snowflake

Covers building and maintaining a Streamlit app that runs inside Snowflake (SiS): the two
runtimes, SiS-specific APIs, partial reruns, cache layering against cold starts, layout/CSS
constraints, and how to evaluate a version upgrade.

> Two distinct context7 libraries cover this surface — query whichever is relevant:
> `libraryId: /streamlit/docs` for Streamlit's own widgets/API, and
> `libraryId: /websites/snowflake_en` for the Snowflake-in-Streamlit integration layer
> (`st.connection`, secrets, caller's rights, runtime differences). Streamlit ships new releases
> roughly monthly — any version number in the reference below (e.g. "1.52.0+") marks when a
> feature was introduced at distillation time, not current behavior. Don't trust a specific version
> claim without checking context7 first.

## References

- `references/streamlit.md` — Container vs Standard runtime comparison, SiS vs desktop Streamlit
  constraints, upgrade evaluation methodology, layout/CSS constraints (what's native vs a hack),
  SiS-specific APIs, `st.fragment` partial reruns, cache layering for SPCS cold starts, gotchas.
- `references/review.md` — the SiS review checklist (page config, session injection, callback
  discipline, cache, module-scoped CSS), applied on top of `pc-core:review-guidelines` when
  reviewing a Streamlit app that runs inside Snowflake.
