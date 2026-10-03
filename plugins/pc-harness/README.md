# pc-harness

Claude Code harness knowledge measured in real sessions — public, generic across any project that
builds plugins, hooks or mods for Claude Code.

## Overview

The docs and the bundled `plugin-authoring` skill describe the API; they do not say where it
surprises. Each entry here was measured on a named build, with the debug log or the API typings
behind it, and is read before building on that part of the harness rather than rediscovered.

## Skills

- `harness` (model-invoked) — mods, plugin and MCP reloading, tools missing from a session,
  testing for real; one reference file per topic under `skills/harness/references/`.
