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
  quality gates (mypy, bandit, interrogate, darglint) only run against `plugins/*/hooks/`.
- A profile-resolved value (chat language, default branch) is never hardcoded into a public
  skill. Each consuming repo declares its own values in its own `.claude/policies.json`.
