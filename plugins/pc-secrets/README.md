# pc-secrets

Keeps secret-shaped content out of the cloud model. A `PreToolUse` hook denies a `Read` — or a
read-like `Bash` command (`cat`, `head`, `tail`, `less`, `more`, `strings`) — whose target file
content carries secret-shaped data, before the content enters the context. A band above the prompt
lets the person choose the outcome of a flagged `Read`.

## Overview

Honest scope: the guard stops the accident, not determined extraction — `grep`, `awk` and
interpreters are open channels by design. The real barriers stay a repo's own pre-commit secret
gates and its `permissions.deny` baseline; path-level protection (`.env`, `~/.ssh`, `~/.aws`)
belongs there too. This hook is the *content* level: real credentials inside an otherwise allowed
file.

- Detection is `hooks/secret_patterns.py`, the one "secret shapes" module (vendored from
  `pinky-bootstrap`; `pc-kanban` carries its own copy).
- A line carrying `# pragma: allowlist secret` or `gitleaks:allow` is not flagged, exactly like the
  pre-commit secret hooks. A file git-tracked and identical to `HEAD` is not re-scanned.
- The agent's own scope is never scanned: a path under the resolved config dir
  (`CLAUDE_CONFIG_DIR`, else `~/.claude/`) or with a `.claude` component, memory notes included.
- The hook never raises: a guard must not block a tool call on its own bug.

## Read band

For a `Read` the guard flags, a mod (`hooks/secrets-hold/register.tsx`, written for the Claude Code
mods API, early access, measured on 2.1.288) holds the call and draws a framed band above the
prompt: `⚿ Secret-shaped data`, the full path and the flagged line numbers, then one button per
outcome (the first press wins):

- `✎ add pragma` (`3`) writes `# pragma: allowlist secret` at the end of each flagged line, then
  lets the read go through the guard, which no longer flags them; Claude is told which lines were
  marked. **It edits the file and permanently allowlists the line** — the same marker the
  pre-commit secret gate honours, so a real secret marked this way also passes that gate later.
  Offered only for a symlink-free file whose format takes a trailing `#` as a plain comment
  (`.py .sh .bash .zsh .yaml .yml .toml .conf .txt .md .env .rb .pl .r`, no extension); never for
  `.ini`/`.properties` (the marker would enter the value) nor for a language where `#` is a syntax
  error. The write is refused when the file changed since the verdict (a content digest travels
  with it), on a line no longer carrying a secret, and on a line continued with `\`.
- `⌂ delegate offline` (`4`) refuses the `Read` and tells Claude to run the task with the `offline`
  tool of `pc-core`, which keeps the content on the machine. Offered only when the local model
  server (`pi`'s `mlx` provider in `~/.pi/agent/models.json`, loopback hosts only) answers within one second.
- `✘ deny` (`9`) refuses the `Read`; Claude asks what to do instead.

The guard stays the source of the verdict: the mod asks `privacy_egress.py --verdict`, never judges
by itself, and writes through `privacy_egress.py --pragma`. A read the engine denies or a settings
rule decides is not held. When nobody answers within one minute,
when the verdict cannot be read, or when the session draws on neither a terminal nor the desktop
app (`claude -p`, the VS Code extension alone), nothing is held and the guard's deny applies. A
`Bash` channel is never held: its deny stays.

The mod is TypeScript, which the repo's gates do not run. Check it with
`claude plugin validate plugins/pc-secrets`, `claude plugin test plugins/pc-secrets` and
`tsc -p plugins/pc-secrets` (the config it extends is written by Claude Code the first time the
plugin loads from a folder you own).

## Requirements

Python 3 for the hook. The `delegate offline` button needs `pc-core` (its `offline` tool) and a
local model server.

## Upgrading from pc-core

The guard used to ship in `pc-core`. It is now this plugin: install `pc-secrets` alongside `pc-core`,
or the secret-read guard is gone silently.
