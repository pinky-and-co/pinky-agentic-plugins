# ATTRIBUTIONS — running ledger (WIP)
*Update date : 2026-08-20 15:26*

> **Why this file exists.** A NOTICE/CREDITS rebuilt at the end (archaeology) loses attributions.
> So we log every external source **as we draw on it**, here, all along the repo's life. A future
> `notice-licenses-scrub`-equivalent card just aggregates and verifies this ledger — it does not
> reconstruct it.
>
> **Rule of thumb — append a row whenever you take from an external source**, classified by *how used*:
> - **inspiration** (concept/format/pattern, no code copied) → courtesy credit, no license obligation.
> - **dependency** (a tool/library we call or bundle) → license obligation if distributed; verify + cite.
> - **adapted** (text/code/knowledge derived from a source) → license obligation; verify + cite.

## Adapted — derived content

| Source | What we took | License | Where it lands |
|---|---|---|---|
| smixs/awesome-claude-output-styles, `hooks/style-reminder.sh` | `UserPromptSubmit` per-turn reminder logic for a custom output style — ported shell → Python (stdlib, Windows-safe), logic unchanged | MIT (repo LICENSE, verified 2026-08-19) | `plugins/pc-core/hooks/style_reminder.py` |
