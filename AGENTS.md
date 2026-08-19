# AGENTS.md — pinky-agentic-plugins

Public marketplace of generic Claude Code plugins for the pinky-and-co suite — reusable workspace
mechanics with zero profile-resolved values (no product codename, no employer reference, nothing
that only makes sense inside one specific org). Anyone can install a plugin from here via
`claude plugin marketplace add`; private, org-specific knowledge lives in separate plugin repos
that depend on this one without duplicating it.

Content here is neutral and tool-agnostic — readable by any agentic tool (Claude Code, Codex,
Cursor…), no tool-specific mechanics (no slash-command name, no literal model id).

## Branch strategy

GitHub Flow — the resolved value the skills rely on
lives in `.claude/policies.json`, this is orientation only.

## Naming glossary

- **Plugin** — a folder under `plugins/<name>/` with its own `.claude-plugin/plugin.json`,
  published through the root `.claude-plugin/marketplace.json`.
- **`pc-*`** — namespace prefix for every plugin here ("pinky & co").
- **Skill** — replaces the legacy `commands/*.md` format entirely; a `commands/` folder anywhere
  in a plugin is a hard failure, not a gap to fill in later.

## Repo conventions

- `scripts/hooks/` is distributed from the moteur, never hand-edited here. This repo's own
  quality gates (mypy, bandit, interrogate, darglint) run against every authored `.py` under a
  plugin — `hooks/` (concern folders nested inside it included) and `skills/<name>/` alike.
- A profile-resolved value (chat language, default branch) is never hardcoded into a public
  skill. Each consuming repo declares its own values in its own `.claude/policies.json`.
- **Package by feature, not by artefact type — within the harness's own root directories.**
  `hooks/` and `skills/` are the two plugin-root directories the harness itself recognises
  (`hooks/hooks.json`, `skills/<name>/SKILL.md`); every hook/script finds its place inside one of
  those two, never a sibling top-level folder like a generic `scripts/`.
  - A script used by exactly one skill lives in that skill's own `scripts/` subfolder — the
    harness's own convention, exposed to the skill body as `${CLAUDE_SKILL_DIR}/scripts/<file>`
    (e.g. `skills/permission-change/scripts/guard.py`). The coupling is then visible from `ls`,
    not just from an `allowed-tools` line buried in `SKILL.md`.
  - A script shared by several skills (or a skill plus an automatic hook) gets its own named
    concern folder nested inside `hooks/` (`hooks/branch-policy/`), referenced from `hooks.json`
    and any skill body via `${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/<file>`.
  - `hooks/` directly (not nested) is reserved for what's genuinely automatic-only, with zero
    skill caller (`inject_policies.py`, `read_cost.py`) — plus `policy_lib.py`, the one library
    both `hooks/` directly and `hooks/branch-policy/` import, kept neutral because it belongs to
    neither concern alone.
  - Decided 2026-08-19 (`deux-hooks-branche-et-cout-de-lecture`): an invisible skill→script
    coupling is debt, and directory structure — not prose — is what a reader checks first.
