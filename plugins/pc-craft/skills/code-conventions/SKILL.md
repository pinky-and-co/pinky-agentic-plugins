---
name: code-conventions
description: Generic engineering conventions for a multi-repo/multi-lib suite — no-framework web app layout (HTML/CSS/JS), library API surface (public vs module-private), Python CLI help and output (--help, status, color, prompts), and third-party bug reproduction tracking. Use when writing or reviewing a no-framework web app, a library's API surface, a Python CLI, or a third-party bug reproduction. No infra content yet.
user-invocable: false
metadata:
  source: original + cli-developer skill (Jeffallan/claude-skills, MIT) — color/TTY layer of references/cli.md adapted
  updated: 2026-09-23
---

# Engineering conventions

Conventions that generalize across any repo of the suite — not tied to one product or one language.
Each reference below is independent; load whichever fits the task.

## References

- `references/web.md` — no-framework static web app conventions: repo layout, CSS layer
  architecture, JS file responsibilities (state/engine/app split), minification and CDN-exclusion
  pattern, pre-commit checklist.
- `references/lib-api.md` — how a library in a multi-lib suite exposes its symbols: public
  (re-exported at the package root) vs module-private (a first-party inter-lib contract), and the
  two anti-patterns to avoid.
- `references/cli.md` — Python CLI conventions: `--help` (summary, per-option defaults, examples),
  status ordering (action first, success collapsed to one line), color/glyphs as a hint (TTY,
  `NO_COLOR`, theme), the plan → confirm → report vocabulary, non-interactive/interrupted runs, the
  stdlib-vs-dependency decision, and testing a renderer with an output oracle.
- `references/repro.md` — convention for documenting a bug hit in a third-party dependency: the
  `_repro/<bug-slug>/` folder structure, the `bug.md` template, status lifecycle.
