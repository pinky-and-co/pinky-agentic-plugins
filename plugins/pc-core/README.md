# pc-core

Session-hygiene hooks, convention replay and a cross-model council — generic workspace mechanics for Claude Code.

## Overview

pc-core ships three skills, four hooks and one UI mod. Two hooks resolve each repo's own policies from `.claude/settings.pinky.json` (chat language, branch workflow, directory structure) and inject them right before the tool call that produces the artefact; two more keep a custom output style reinforced every turn and run the just-edited file's own repo checkers in the same turn. The skills cover the repetition side of engineering work: replaying a decided change onto every remaining repo, pressure-testing a decision through five models, and one shared review methodology.

## Skills

### `apply-pattern`

Replay a change already made on one witness target onto the remaining targets: the witness diff is the specification — this skill applies it, it never re-decides it. Use after doing a convention/refactor for real on one repo, to propagate it without pasting prompts into N sessions. Each successfully applied target is committed and pushed, with a PR/MR opened through the branch-policy `bulk_commit.sh`.

```
/apply-pattern repo-a --rest
```

### `council`

Run a question or decision through a council of 5 advisors on 5 models: independent analysis, anonymous peer review, then a chairman's verdict cross-checked on one more model. Takes a type: `generic` (any decision), `tech` (architecture, writes an ADR), `pitch` (product concept, writes founding docs). Cross-model dispatch runs through the `panel` MCP tool of the `pc-models` plugin; when the pool cannot field five distinct families, the unfilled lenses fall back to Claude sub-agents, traced as such.

```
/council tech should the parser live in the core library or its own package?
```

### `review-guidelines` *(reference)*

Shared review methodology — severity-labeled findings, high precision, a clear verdict, an optional adversarial stance. Use whenever reviewing code changes or a described claim/result, as the `reviewer` agent, the `adversarial` MCP tool or any other reviewer. Stack-specific guidance (FastAPI, Streamlit, Snowflake…) lives in the dedicated skills.

## Hooks

- `style_reminder.py` — UserPromptSubmit, every user prompt: re-emits the per-turn reminder naming the active custom output style. Claude Code emits that reminder only for its built-in styles; a custom style is placed in the system prompt once, and this hook keeps its voice present over a long session.
- `inject_policies.py` — PreToolUse on `Bash|Write|Edit`: detects what the call is about to produce (a commit message, a PR/MR body, code, docs…) and injects the per-repo resolved policies (language, branch workflow) from `.claude/settings.pinky.json` into the session.
- `structure_guard.py` — PreToolUse on `Write`: denies a write creating a file outside the repo's own declared `file_placement` convention (in `.claude/settings.pinky.json`). A repo that never declares the key is never blocked.
- `post_edit_lint.py` — PostToolUse on `Edit|Write|MultiEdit|NotebookEdit`: runs the checkers of the edited file's own repo (`scripts/hooks/*.py`, the same ones pre-commit runs at commit time) and surfaces their findings in the same turn — informs, never blocks.
- `register.tsx` (mod, with `hooks/subagent-routing/`) — holds each Agent call whose model the session chooses: a classifier ranks the task, the proposed model and effort show in a band above the prompt, and the call leaves with the user's pick — or with the proposal after 60 s, or where no band is drawn.

## Requirements

- Python 3.12 or newer, resolved as `python3` on the `PATH` — stdlib only — for the four hooks.
- `git`, plus `gh` (GitHub) or `glab` (GitLab) matching the remote provider, for `apply-pattern`'s commit and PR/MR step (`bulk_commit.sh`).
- The `panel` MCP tool of the `pc-models` plugin for `council`'s cross-model advisors — optional: without it, every advisor runs as a Claude sub-agent.
- The `subagent-routing` mod is TSX loaded by Claude Code itself (it imports the harness's own `claude-code` module; no package.json, no build step).

## Dependencies

None.
