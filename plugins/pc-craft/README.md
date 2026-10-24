# pc-craft

Engineering conventions for a multi-repo suite — how code and reviews are written, beyond what a linter can check.

## Overview

Four convention skills covering distinct concerns: generic engineering rules that apply to any repo of the suite, a structured brief for bug reports, FastAPI-specific review guidance, and a design-review pass that surfaces architectural friction. Reference skills load on demand when their subject comes up; the two user-invocable ones are triggered by an explicit ask.

## Skills

### `code-conventions` *(reference)*

Generic conventions for a multi-repo suite: no-framework web app layout (HTML/CSS/JS), library API surface (public vs module-private), Python CLI help and output (`--help`, status, color, prompts), third-party bug reproduction tracking.

### `debug-brief`

Assembles a self-contained debug brief — a reported behavior discrepancy confronted with the code, an optional spec and a sample of real data — so a model can reason about root cause. One text block to paste into an external model or send through the `panel` MCP tool.

```
"X does the wrong thing when …" + debug-brief
```

### `fastapi` *(reference)*

FastAPI review conventions — dependency injection, Pydantic input/output model separation, async correctness, authorization vs authentication, request-scoped DB sessions, pagination.

### `improve`

Surfaces architectural friction in a repo (shallow modules, scattered locality, hidden seams) using the deletion test, writes an HTML report, then grills the deepening candidate the user picks. Not a bug hunt, not a style pass.

```
"review this repo's design" + improve
```

## Requirements

None — prompt-only skills. The optional `panel` send in `debug-brief` needs `pc-models` installed.

## Dependencies

`pc-core`.
