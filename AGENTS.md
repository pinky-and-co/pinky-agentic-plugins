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
  `hooks/`, `skills/`, and `mcp/` are the plugin-root directories the harness itself recognises
  (`hooks/hooks.json`, `skills/<name>/SKILL.md`, `.mcp.json` declaring an `mcp/` server); every
  hook/script finds its place inside one of those, never a sibling top-level folder like a generic
  `scripts/`.
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
  - **Agents never bundle their own script.** Verified against the 8 `agents/`-carrying plugins of
    the official marketplace: every `agents/*.md` there is a flat, self-contained prompt file, zero
    exceptions — no plugin ships a script an agent shells out to. An agent needing deterministic
    work behind it is a signal to route that work through `mcp/` instead (below), not to invent an
    `agents/scripts/` folder with no precedent anywhere in the ecosystem.
  - **`mcp/` holds one MCP server's implementation** (`mcp/server.py` + whatever sibling modules it
    imports, e.g. `pc-core`'s `model_offline.py`/`model_adversarial.py`), declared via a `.mcp.json`
    at the plugin root (`{"<server-name>": {"command": "uv", "args": ["run", "--script",
    "${CLAUDE_PLUGIN_ROOT}/mcp/server.py"]}}`). This is the right layer whenever a tool needs to be
    natively callable (by the main session or any subagent, no spawn) rather than wrapped in an
    agent that shells out and relays — see `deux-hooks-branche-et-cout-de-lecture`'s sibling
    decision, `agents-provider-routing-offline-adversarial` (2026-08-19): an agent that does no
    reasoning of its own, only shelling + relaying, pays for a full subagent spawn for nothing.
    `uv run --script` with PEP 723 inline `dependencies` resolves the server's own third-party
    packages (`mcp` itself included) with zero pip-install step for the plugin installer — this
    repo otherwise ships no runtime dependency mechanism at all (`pyproject.toml`: nobody
    `pip install`s a Claude Code plugin), so a hard `import mcp` without that resolution path would
    break for every installer. `uv` is therefore a real prerequisite for any plugin that ships an
    `mcp/` server, on top of Python.
  - **`scripts/` holds a status line's command scripts** (`pc-statusline/scripts/statusline.py`
    + `subagent_statusline.py` + a shared module). A status line is not a hook (not in
    `hooks.json`), a skill, an agent, or an MCP server — it is a command wired through a
    `settings.json` key (`statusLine` by `pinky install --plugin`, `subagentStatusLine` by the
    plugin's own bundled `settings.json`, `${CLAUDE_PLUGIN_ROOT}/scripts/<file>`). This is the one
    sanctioned `scripts/` directory; every other script still lives in `hooks/`,
    `skills/<name>/scripts/`, or `mcp/`.
  - Decided 2026-08-19 (`deux-hooks-branche-et-cout-de-lecture`): an invisible skill→script
    coupling is debt, and directory structure — not prose — is what a reader checks first.
