---
name: improve
description: "Surface architectural friction in a codebase and propose deepening opportunities (shallow modules, scattered locality, hidden seams) using the deletion test; writes an HTML report, then grills the candidate the user picks. Use when asked to review a repo's design, find refactor opportunities or assess module depth. Not a bug hunt, not a style pass."
user-invocable: true
allowed-tools: Read, Write, Edit, Bash(rg:*), Bash(rtk rg:*), Bash(find:*), Bash(rtk find:*), Bash(ls:*), Bash(rtk ls:*), Bash(git rev-parse:*), Bash(git -C * rev-parse*), Bash(date:*), Bash(basename:*)
metadata:
  source: original — method after Ousterhout, "A Philosophy of Software Design"
  updated: 2026-08-27
---

# Improve

Find where a codebase's design works against its callers, and propose changes that concentrate
complexity instead of scattering it. The verdict is per-candidate and evidence-backed.

## Vocabulary — use these terms exactly

| Term | Meaning | Avoid |
|---|---|---|
| **module** | anything with an interface and an implementation | unit, component, service |
| **interface** | everything a caller must know: types, invariants, error modes, constraints | API, signature |
| **depth** | behaviour a caller gets per unit of interface learned | — |
| **seam** | where an interface lives; where behaviour can shift without editing in place | boundary |
| **adapter** | concrete thing satisfying an interface at a seam | — |
| **locality** | change and bugs concentrate in one place rather than spreading across callers | — |
| **deletion test** | imagine deleting the module: does complexity concentrate or scatter? | — |

## Step 0 — read the repo's own decisions first

1. Review the docs under `docs/` — the doc map.
2. Read every ADR under `docs/adr/` — note status (accepted / superseded).
3. Read `CONTEXT.md` if present — the canonical terms for this repo.
4. **Never re-debate an accepted ADR.** Flag the conflict explicitly, then skip that candidate
   unless the user asks to revisit.

## Auto context

- Docs: `rg --files -g "*.md" docs 2>/dev/null | sort`
- ADRs: `rg --files -g "*.md" docs/adr 2>/dev/null | sort`
- `CONTEXT.md`: `find . -maxdepth 1 -name "CONTEXT.md" 2>/dev/null`
- Repo: `basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"`
- Timestamp: `date +%Y%m%d-%H%M`

## Three-phase process

### Phase 1 — Explore

Walk the codebase noting:

- **Shallow modules** — interface nearly as complex as the implementation.
- **Scattered locality** — a change requires editing N files instead of 1.
- **Hidden seams** — a natural variation point with no interface in front of it.

Apply the **deletion test** to each candidate: deleting it scatters complexity across callers → it
earns its keep. Deleting it removes a pass-through → it's likely the wrong shape.

### Phase 2 — HTML report

Write findings to `<scratchpad>/improve-<repo>-<timestamp>.html` — one self-contained file, inline
CSS, system font stack, light background. No CDN (the page must open offline).

Each candidate card:

- Problem (use the vocabulary above).
- Proposed solution with a before/after sketch.
- Benefits framed around **locality** and **leverage**.
- Recommendation strength: `strong` / `moderate` / `weak`.
- ADR conflicts: flag explicitly — never silently skip.

Print the file path when done.

### Phase 3 — Grill

Once the user picks a candidate, pressure-test it — invoke the `pc-kanban:grill` skill if the design
is large enough to warrant a full interview, otherwise walk it inline:

- Walk design constraints, dependencies, and test surfaces together.
- Update `CONTEXT.md` with terms that crystallised during the session.
- Propose an ADR only if the decision is hard to reverse, surprising without context, and involves
  real trade-offs.

## Key principle

> "The interface is the test surface." Depth and testability rise together when a small interface
> hides substantial, locality-rich behaviour.
