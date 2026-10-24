# pc-secrets

Keeps secret-shaped content out of the cloud model: denies a `Read`, or a read-like `Bash`
command, whose file carries a secret, and offers the person a choice on a `Read`.

## Overview

The plugin ships a `PreToolUse` guard (`hooks/privacy_egress.py`), the secret-shape detector it
runs on (`hooks/secret_patterns.py`) and a band mod (`hooks/secrets-hold/register.tsx`) that holds
a flagged `Read` above the prompt for the person to decide. The guard is the *content* level:
real credentials inside an otherwise allowed file — path-level protection (`.env`, `~/.ssh`,
`~/.aws`) belongs to a repo's own `permissions.deny` baseline, and the real barriers stay that
baseline and the repo's pre-commit secret gates. Honest scope: the guard stops the accident, not
determined extraction — `grep`, `awk` and interpreters are open channels by design.

Detection honours the same opt-out markers as the pre-commit secret hooks: a line carrying
`# pragma: allowlist secret` or `gitleaks:allow` is not flagged, and a file git-tracked and
identical to `HEAD` is not re-scanned. The agent's own scope is never scanned: a path under the
resolved config dir (`CLAUDE_CONFIG_DIR`, else `~/.claude/`) or with a `.claude` component,
memory notes included, is skipped before any content check. The hook never raises: a guard must
not block a tool call on its own bug.

`pc-core` carries no secret-read guard — install `pc-secrets` alongside `pc-core`, or the
secret-read guard is silently absent.

## Skills

None — the plugin ships hooks and a mod only.

## Hooks

- **`privacy_egress.py`** — `PreToolUse` on `Read|Bash` (10 s timeout). Denies a `Read` — or a
  read-like `Bash` command (`cat`, `head`, `tail`, `less`, `more`, `strings`) — whose target file
  content carries secret-shaped data, before the content enters the context. Detection comes from
  `hooks/secret_patterns.py`, the one "secret shapes" module — `pc-kanban` carries its own copy.
  The same script also answers the band mod below:
  `--verdict` prints the guard's judgement of a `Read` event as JSON (which lines, which choices
  make sense) and `--pragma` writes the allowlist marker on the lines it is given — the guard
  stays the one source of the verdict; the mod only presents it.
- **`secrets-hold/register.tsx`** — a mod (Claude Code mods API, early access, measured on
  2.1.288) registered under the same `hooks.json` (`modules`). For a `Read` the guard flags, it
  holds the call and draws a framed band above the prompt: `⚿ Secret-shaped data`, the full path
  and the flagged line numbers, then one button per outcome (the first press wins):
  - `✎ add pragma` (`3`) writes `# pragma: allowlist secret` at the end of each flagged line,
    then lets the read go through the guard, which no longer flags them; Claude is told which
    lines were marked. **It edits the file and permanently allowlists the line** — the same
    marker the pre-commit secret gate honours, so a real secret marked this way also passes that
    gate later. Offered only for a symlink-free file whose format takes a trailing `#` as a plain
    comment (`.py .sh .bash .zsh .yaml .yml .toml .conf .txt .md .env .rb .pl .r`, no extension);
    never for `.ini`/`.properties` (the marker would enter the value) nor for a language where
    `#` is a syntax error. The write is refused when the file changed since the verdict (a
    content digest travels with it), on a line no longer carrying a secret, and on a line
    continued with `\`.
  - `⌂ delegate offline` (`4`) refuses the `Read` and tells Claude to run the task with the
    `offline` tool of `pc-models`, which keeps the content on the machine. Offered only when the
    local model server (`pi`'s `mlx` provider in `~/.pi/agent/models.json`, loopback hosts only)
    answers within one second.
  - `✘ deny` (`9`) refuses the `Read`; Claude asks what to do instead.

  A read the engine denies or a settings rule decides is not held. When nobody answers within one
  minute, when the verdict cannot be read, or when the session draws on neither a terminal nor
  the desktop app (`claude -p`, the VS Code extension alone), nothing is held and the guard's
  deny applies. A `Bash` channel is never held: its deny stays.

  The mod is TypeScript, which the repo's gates do not run. Check it with
  `claude plugin validate plugins/pc-secrets`, `claude plugin test plugins/pc-secrets` and
  `tsc -p plugins/pc-secrets` (the config it extends is written by Claude Code the first time the
  plugin loads from a folder you own).

## Requirements

Python 3.12 or newer, resolved as `python3` on the `PATH`, for the hook. The `delegate offline`
button needs `pc-models` (its `offline` tool) and a local model server.

## Dependencies

None.
