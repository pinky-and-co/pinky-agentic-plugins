# ATTRIBUTIONS — running ledger
*Update date : 2026-08-31 13:10*

> **Carried over from pinky-workspace** (`docs/ATTRIBUTIONS-WIP.md`) when its engine migrated into
> these plugins. This stays the append-as-you-go ledger — every external source logged the moment
> it is drawn on, so a NOTICE rebuilt by archaeology never loses one.
>
> **Per-repo aggregation + verification** = kanban card `notice-licenses-scrub-plugins`, which
> aggregates and verifies this ledger into per-repo `NOTICE`/`CREDITS`. It is **BLOCKING before any
> plugin repo goes public** (the repos are private today). It does not reconstruct this ledger.
>
> Paths in the "Where it lands" / "Status" columns below are **pinky-workspace paths** — the scrub
> card maps each migrated source to its plugin home (e.g. `core/rules/base/minimal-code.md` →
> `pinky-bootstrap/agentic-harness/rules/pc-core/minimal-code.md`; the 4 council skills →
> `pc-core:council`; `read-documents` rule → `pc-core:document-to-md`; `yt` skill → `pc-kanban:yt`).
>
> **Rule of thumb — append a row whenever you take from an external source**, classified by *how used*:
> - **inspiration** (concept/format/pattern, no code copied) → courtesy credit, no license obligation.
> - **dependency** (a tool/library we call or bundle) → license obligation if distributed; verify + cite.
> - **adapted** (text/code/knowledge derived from a source) → license obligation; verify + cite.

## Inspirations — concept / format / pattern (no code copied)

| Source | What we took | License (verified 2026-07-05) | Where it lands |
|---|---|---|---|
| Diátaxis (Daniele Procida) | doc folder structure (tutorials/how-to/reference/explanation) | CC-BY-SA (text) — structure only, no obligation | `core/rules/base/doc_conventions.md`, `docs/` |
| LLM Council (Andrej Karpathy) | multi-advisor debate + peer-review + chairman methodology | no LICENSE file in `karpathy/llm-council` (all-rights-reserved default) — methodology/concept only, no code or text copied, no obligation | `llm-council` skill / `commands/llm_council.md` |
| gstack (garrytan/gstack) | council décision→artefact pattern ; `/careful` `/freeze` `/guard` `/investigate` skill design ; "taste decisions → human, rest auto-fixes" | MIT (GitHub API confirmed) | cards `council-variants`, `skills-careful-freeze` |
| ECC (affaan-m/ECC) | format ideas ONLY (not adopted): "specific overrides general", hooks "how-to-fix" column, runtime hook-disable env var ; `the-security-guide.md` notable | MIT (GitHub API confirmed) | cards `ecc-format-inspirations` ; SPEC § agentic security |
| ponytail (DietrichGebert/ponytail) | concept ONLY (plugin NOT installed): the pre-generation decision ladder (YAGNI → reuse → stdlib → native → installed dep → one line → MVP) + "minimal ≠ unsafe" guardrails | MIT (GitHub API confirmed) | `core/rules/base/minimal-code.md` |
| ADHD (UditAkhourii/adhd) | pattern ONLY (npm pkg NOT installed): the **pre-flight cost gate** for an expensive skill — explicit-invocation bypass, 3-question self-judge, abort with a one-line re-offer. Text rewritten for the councils, not copied. The frames table + generator/critic split are noted as ideas, NOT adopted (see card `council-voix-cross-modele`) | MIT (GitHub API confirmed 2026-07-22) | `pre-flight` section of the 4 council skills (`llm-council`, `council-tech`, `council-pitch`, `council-suite`) |

## Dependencies — tools/libraries (license matters if distributed)

| Source | What we took | License (verified 2026-07-05) | Status |
|---|---|---|---|
| markitdown (Microsoft) | convert-then-read for office/HTML formats | MIT (GitHub API confirmed) | optional dep — `read-documents` rule |
| yt-dlp | fetch auto-generated subtitles (VTT), no video download | Unlicense (GitHub API confirmed) | optional dep — `yt` skill |
| RTK (rtk-ai/rtk) | rewrites Bash commands to a token-filtered form before they run | Apache-2.0 (`brew info rtk`) | optional dep — plugin `pc-rtk` |
| AgentShield (affaan-m/agentshield) | one-shot pre-distribution config audit | MIT (GitHub API confirmed) | backlog, NOT a runtime dep |
| markdownify · beautifulsoup4 · lxml | HTML→lean-MD conversion in `scripts/mirror_to_md.py` (select `<article>`, render to MD) | MIT · MIT · BSD-3-Clause (PyPI metadata confirmed) | generation-time dep only, not shipped in any lib |
| trafilatura | HTML→lean-MD candidate (⭐ in card) | n/a | **rejected** (ADR-0017): drops Sphinx signature/params on API pages |
| html2text · pandoc · docling | HTML→MD alternatives evaluated | n/a | evaluated, not adopted |

## Adapted — derived content (the core of notice-licenses-scrub)

| Source | What we took | License | Status |
|---|---|---|---|
| Snowflake official docs | 13× `corpus-public/knowledge/AGENT_SNOWFLAKE_*.md` distilled/rewritten (own prose, tables, code examples — not verbatim) | proprietary vendor docs, no open license found; treated as adaptation given transformative rewriting (**user-confirmed call, 2026-07-05**) | **closed** — Source/License line added to all 13 files. Migré : skills `pc-snow` (`metadata.source`) — à re-confirmer au scrub |
| Streamlit official docs | `AGENT_SNOWFLAKE_STREAMLIT.md` (partial) | Apache-2.0 (`streamlit/docs` repo, GitHub-confirmed) — adaptation with attribution explicitly permitted | **closed**. Migré : `pc-snow:streamlit` |
| Snowflake + Streamlit raw wget mirrors + lean-MD (`docs/api/…`) | local reference material only | n/a — **never shipped** (`.gitignore`) | **closed** — zero exposure, no obligation |
| *task-observer* meta-skill — "One Skill to Rule Them All", Eoghan Henn / rebelytics.com | two observation types → `core/rules/base/proactivity.md` §§ 2–3 | CC BY 4.0 (per source) — **attribution required** | **TBD** — logged 2026-07-28. Migré : rules kanban-proactivity (`pc-kanban`). Credit must land in NOTICE/CREDITS before public |

## Scrub status

- **2026-07-05 — closed.** All TBD rows of the time resolved. NOTICE + CREDITS produced at
  pinky-workspace root.
- **2026-07-28 — reopened, 1 TBD.** The *task-observer* row (CC BY 4.0).
- **2026-08-31 — scrub done for the plugin repos** (card `notice-licenses-scrub-plugins`). Verdict
  per source, checked against the current plugin repos:

  | Source | Verdict | Home |
  |---|---|---|
  | markitdown (MIT) | NOTICE — optional runtime dep | `pinky-agentic-plugins` NOTICE — `pc-core:document-to-md` |
  | yt-dlp (Unlicense) | NOTICE — courtesy (public domain) | `pinky-agentic-plugins` NOTICE — `pc-kanban:yt` |
  | RTK (Apache-2.0) | NOTICE — optional runtime dep | `pinky-agentic-plugins` NOTICE — plugin `pc-rtk` |
  | Snowflake docs (adapted) | NOTICE — transformative adaptation | `pinky-agentic-plugins` NOTICE — `pc-snow` skills, `metadata.source` per file |
  | Streamlit docs (Apache-2.0, adapted) | NOTICE — attribution given | `pinky-agentic-plugins` NOTICE — `pc-snow:streamlit` |
  | markdownify · beautifulsoup4 · lxml | NOTICE — generation-time only, not shipped | `pinky-bootstrap` NOTICE — `tools/mirror_to_md.py` |
  | LLM Council / Karpathy | CREDITS — concept, no obligation | `pinky-agentic-plugins` CREDITS — `pc-core:council` |
  | gstack (MIT) | CREDITS — concept/design, no code | `pinky-agentic-plugins` CREDITS — `pc-kanban:permission-change`, `pc-core:council` |
  | ADHD (MIT) | CREDITS — pattern, text rewritten | `pinky-agentic-plugins` CREDITS — `pc-core:council` pre-flight |
  | ponytail (MIT) | CREDITS — concept, no code | `pinky-bootstrap` CREDITS — rule `minimal-code.md` |
  | ECC (MIT) | CREDITS — "specific overrides general" phrase/concept | `pinky-bootstrap` CREDITS — rule `minimal-code.md` |
  | Diátaxis (CC-BY-SA text) | CREDITS — folder structure only, text not reproduced | both repos' CREDITS |
  | **task-observer (CC BY 4.0)** | **N/A — the derivative was removed.** The `proactivity.md` rule was deleted in the plugin migration (card `migration-des-24-rules-verdict-par-fichier`, line 29 — "supprimées, condensées en une puce"); the current `behavior` config carries none of its two observations. No distributed derivative → no attribution obligation. | — |
  | AgentShield · trafilatura · html2text · pandoc · docling | N/A — never adopted | — |

- **`pinky-agentic-plugins-private`** — no external-derived content (proprietary domain knowledge
  only); no NOTICE/CREDITS needed.
- **Blocking-before-public**: re-run this table against each repo's actual state right
  before flipping any repo public — the plugin set moves.
