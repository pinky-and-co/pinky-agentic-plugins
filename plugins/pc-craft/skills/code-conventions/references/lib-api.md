# Library API surface

How a library in a multi-lib suite exposes its symbols: **public** (re-exported at the package
root) vs **module-private** (a first-party inter-lib contract). Read this before adding a function
that another repo in the suite will call. Not about a single lib's feature list — that belongs to
each lib's own documentation.

---

## Two API levels

A symbol defined in a lib module falls into exactly one of two levels — the leading underscore is
the switch:

| Level | Marker | Where it lives | For whom | Stability |
|---|---|---|---|---|
| **Public** | no `_` | re-exported at the package root (`__all__`), `from lib import name` | external users of the lib | versioned, guaranteed |
| **Module-private** | `_` prefix | the module only, `from lib.mod import _name` | a **first-party sibling lib** of the same suite | none for external users; coupling assumed |

- **Public** = a capability advertised to whoever installs the lib. It must be re-exported at the
  package root and listed in `__all__` — a regression test enforces that every non-`_` name a
  module defines is importable from the package top level, so the advertised surface never drifts.
- **Module-private** = an internal helper that is *nonetheless* shared across repos of the same
  suite. It stays out of the top-level API but is imported **by module path** by a first-party
  consumer (`from lib_a.mod import _helper`). The two repos are versioned and released together, so
  the coupling to a `_`-name is deliberate and safe — it is a *contract between sibling libs*, not a
  public promise.

## Choosing the level — decision

- Will an **external** user (someone who just `pip install`s the lib, not a sibling repo) call it? →
  **public** (no `_`, re-export it).
- Is it plumbing that only a **sibling lib** of the suite consumes, over a coupling you control and
  release together? → **module-private** (`_` prefix, sibling imports it by module path).
- Unsure? Default to **module-private**. Promoting a `_`-name to public later is additive and safe;
  demoting a published public name is a breaking change.

## Two anti-patterns

- **Never make a symbol public just to satisfy the re-export regression test.** If it is an internal
  contract, prefix it `_` — the test then correctly ignores it. Widening the advertised API to
  silence a test is exactly backwards.
- **Never let a sibling lib reach into a *non-underscore* internal by module path.** If a sibling
  needs it, that's a signal it is a real contract → give it the `_` prefix and treat it as one, or
  promote it to public if it is genuinely user-facing.

## Enforcement hooks (consistent with the above)

- A **public-API regression test** asserts every non-`_` module name is re-exported at the package
  root. `_`-names are ignored — they are not part of the advertised surface.
- A **typed-API hook** that forbids raw containers in public annotations **skips `_`-prefixed
  functions**. Same logic: private symbols are not the advertised surface, so the stricter public
  rules don't apply to them.

Concrete application in a given suite (which repos, which symbols, the decision that froze it)
belongs in that consumer repo's own decision record — not here. This reference is the
provider-agnostic principle only.
