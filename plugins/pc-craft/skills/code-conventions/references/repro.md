# Third-party bug reproduction & tracking (`_repro/`)

Convention for documenting a bug in a third-party dependency (an SDK, an external API) hit during
development. Not for: bugs in this repo's own code (use the normal test + fix workflow) or one-shot
investigation notes with no lasting value.

---

## Why

Without a written rule, each session (or each contributor) reinvents its own ad-hoc way to document
a third-party bug. The pattern below generalizes to any repo that depends on unstable third-party
APIs: isolate the reproduction proof and the verified status in one dedicated folder, separate from
application code.

## Convention

```
_repro/
└── <bug-slug>/
    ├── bug.md         ← status + report (mandatory)
    └── repro.py|.ipynb  ← runnable reproduction (mandatory unless the bug can't be isolated standalone)
```

One folder = one identified third-party bug. `<bug-slug>` is a short kebab-case name for the defect
(e.g. `table-kind-enum`, `eai-secrets-serialization`).

## `bug.md` template

```markdown
# Bug — <component>: <short description>

**Status: OPEN | NOT REPRODUCING | FIXED** (verified YYYY-MM-DD) — <one-line next action>

**Environment:**
\`\`\`
<sdk>: <version>
<server/runtime>: <version>
<language>: <version>
\`\`\`

## Suspected / confirmed defect
What's wrong, and where (module/function if known).

## Steps to reproduce
Runnable snippet or pointer to `repro.py`/`repro.ipynb`.

## Expected / Actual
What should happen vs. what happens (error code, message).

## Suggested fix
What the upstream fix would look like.

## Workaround (if any)
What this repo does in the meantime — link the code that implements it.
```

Status values:
- **OPEN** — confirmed reproducing, not yet fixed upstream. File a Support case / upstream issue if
  a public tracker exists.
- **NOT REPRODUCING** — suspected but disproved on the current SDK/server pairing. Always prove a
  bug reproduces *before* writing a workaround — a workaround for a bug that doesn't trigger is dead
  code.
- **FIXED** — confirmed fixed upstream, verified against the codebase.

## Lifecycle

- **OPEN → FIXED**: once the upstream fix ships and this repo upgrades to the fixed version, verify
  the fix, flip the status, and remove any workaround from application code.
- **FIXED → purge**: purge the folder only once the fixed dependency version is the enforced
  minimum (no rollback path reproduces the bug again). Until then, keep `repro.*` as a **regression
  probe** — re-run it after any SDK/server upgrade to confirm the bug hasn't resurfaced. A repo with
  a long-lived example of this pattern (a bug tracked from OPEN through several upgrade cycles to
  FIXED) is the best proof it's worth the discipline.
- **NOT REPRODUCING**: keep as a probe indefinitely — cheap to keep, and re-running it after an
  upgrade is the only way to know if a previously-suspected bug has started triggering.

## Excluded from `_repro/`

One-shot artefacts meant to be pasted elsewhere (a Support case draft, a forum post) and with no
lasting value once published. Keep them out of the commit — `_repro/` is for probes with ongoing
value, not a drafting scratchpad.
