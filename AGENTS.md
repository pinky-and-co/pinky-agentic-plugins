# AGENTS.md — pinky-agentic-plugins

Public marketplace of generic Claude Code plugins for the pinky-and-co suite — reusable workspace
mechanics with no value specific to one repo or org (no product codename, no employer reference,
nothing that only makes sense inside one specific org). Anyone can install a plugin from here via
`claude plugin marketplace add`; private, org-specific knowledge lives in separate plugin repos
that depend on this one without duplicating it.

Content here is Claude Code plugin content — skills, agents, hooks and MCP servers use Claude Code's
own mechanics. A model is named by its tier (`haiku`, `sonnet`, `opus`) in frontmatter and prompts,
never by a literal model id.

## Branch strategy

GitHub Flow — the resolved value the skills rely on
lives in `.claude/settings.pinky.json`, this is orientation only.

## Plugin versioning

Every plugin stays at `0.1.0` until it is actually released. A feature does not bump anything —
the version moves at a release, not at a merge. Nothing here is published yet, so a version number
that tracks development effort just counts commits.

## Where an installed plugin runs from

This repo is registered as a `directory` marketplace. `${CLAUDE_PLUGIN_ROOT}` then resolves to the
working tree (`plugins/<name>/`), not to the copy under
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` — measured on Claude Code 2.1.283
(a marker added only to the cache copy never reached a skill or a hook; `ps` shows MCP servers
started from `plugins/<name>/mcp/`).

| Component | Runs from | After an edit |
|---|---|---|
| Hook script | working tree | next matching tool call — re-executed every time |
| Skill `scripts/` | working tree | next invocation — run as a fresh process |
| Skill body | working tree | a new session or `/reload-plugins` — a skill already loaded in a session is not re-read |
| MCP server | working tree | `/mcp` → the server → Reconnect — `/reload-plugins` leaves the running process, and its code, in place |
| `hooks.json`, skill/agent frontmatter | working tree | `/reload-plugins` |
| Status line (`pc-statusline`) | **cache** | `pinky claude-plugin refresh pc-statusline` |

The MCP server row is measured: after `/reload-plugins`, `ps` showed the `pc-core` server still
started before the edit and the `adversarial` tool still routed on the old code; `/mcp` → Reconnect
started a fresh process. The `/reload-plugins` rows follow from how each is loaded — a read at
session or reload time — and were not measured one by one.

The status line is the one cache consumer: `statusLine` has no plugin-manifest slot, so
`pinky claude-plugin install` wires an absolute path into `~/.claude/settings.json`, taken from
`installed_plugins.json` — a cache directory carrying the version. `claude plugin install`/`update`
never refresh a cache directory that already exists; `refresh` clears and refetches it. A version
bump lands a new cache directory while the wired path keeps pointing at the old one — no error, no
signal — until `pinky claude-plugin install <name>` re-points it.

When an edit seems ignored, check what actually loaded it before refreshing anything: a skill's
"Base directory" line, `ps` for an MCP server, the `statusLine` path.

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
- A value that differs per repo (chat language, default branch) is never hardcoded into a public
  skill. Each consuming repo declares its own values in its own `.claude/settings.pinky.json`.
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
    skill caller (`inject_policies.py`, `style_reminder.py`) — plus `policy_lib.py`, the one library
    both `hooks/` directly and `hooks/branch-policy/` import, kept neutral because it belongs to
    neither concern alone.
  - **Agents never bundle their own script.** Verified against the 8 `agents/`-carrying plugins of
    the official marketplace: every `agents/*.md` there is a flat, self-contained prompt file, zero
    exceptions — no plugin ships a script an agent shells out to. An agent needing deterministic
    work behind it is a signal to route that work through `mcp/` instead (below), not to invent an
    `agents/scripts/` folder with no precedent anywhere in the ecosystem.
  - **`mcp/` holds one MCP server's implementation** (`mcp/<plugin>_server.py` + whatever sibling
    modules it imports, e.g. `pc-core`'s `model_offline.py`/`model_adversarial.py`), declared via a
    `.mcp.json` at the plugin root (`{"<server-name>": {"command": "uv", "args": ["run", "--script",
    "${CLAUDE_PLUGIN_ROOT}/mcp/<plugin>_server.py"]}}`). The entrypoint is named for its plugin, not
    a bare `server.py`: mypy runs one invocation over every `plugins/*/` file and hard-errors on two
    same-basename modules with no package path (plugin dirs carry hyphens). This is the right layer whenever a tool needs to be
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
    `settings.json` key (`statusLine` by `pinky claude-plugin install`, `subagentStatusLine` by the
    plugin's own bundled `settings.json`, `${CLAUDE_PLUGIN_ROOT}/scripts/<file>`). This is the one
    sanctioned `scripts/` directory; every other script still lives in `hooks/`,
    `skills/<name>/scripts/`, or `mcp/`.
  - Decided 2026-08-19 (`deux-hooks-branche-et-cout-de-lecture`): an invisible skill→script
    coupling is debt, and directory structure — not prose — is what a reader checks first.
