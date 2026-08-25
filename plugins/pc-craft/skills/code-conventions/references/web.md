# Web conventions (HTML / CSS / JS)

Conventions for no-framework static web apps: repo layout, CSS layering, JS file responsibilities,
and minification rules. Agent-actionable.

---

## Repo layout

```
<app-root>/
├── index.html              ← hub entry point — links to pages
├── config.js               ← shared config (URLs, feature flags) — loaded before app.js
├── assets/
│   └── images/             ← shared images
└── src/
    ├── tokens.css          ← CSS custom properties only — no selectors, no components
    ├── style.css           ← design system: @import tokens + shared components
    └── pages/
        └── <slug>/
            ├── index.html
            ├── app.css          ← page-specific styles only
            ├── app.js           ← single JS file when the page is simple
            └── scripts/         ← subdirectory only when multiple JS files are needed
                ├── state.js
                ├── engine.js    ← source — NOT served (see Minification)
                ├── engine.min.js← built artifact — committed and served
                └── app.js
```

---

## CSS — layer architecture

### Layer order (invariant)

| File | Role | Rule |
|------|------|------|
| `src/tokens.css` | CSS custom properties only (`--primary`, `--font-body`, `--r-4`…) | No selectors, no layout, no components |
| `src/style.css` | Design system: `@import './tokens.css'` + shared components (`.btn`, `.section`, `.label`, `.inp`…) | Every page imports this file **first** |
| `pages/<slug>/app.css` | Page-specific component overrides | Never redeclare a token or a component already in `style.css` |

### HTML import order

```html
<link rel="stylesheet" href="../../style.css">   <!-- always first -->
<link rel="stylesheet" href="./app.css">          <!-- page-specific second -->
```

### Theme switching

Via `<body>` class — never inline JS:

```html
<body class="theme-primary">      <!-- default -->
<body class="theme-secondary">
<body class="theme-tertiary">
```

Accent tokens (`--accent`, `--accent-strong`, `--accent-tint`) are resolved by the active theme.
Never hardcode a brand color in a component.

### When to extend `style.css` vs create `app.css`

- Component used on **one page only** → `pages/<slug>/app.css`
- Component reused on **≥ 2 pages** → `src/style.css` (or flag to the maintainer)

---

## JS — file responsibilities

### When a single file is enough

Simple page (no shared state, no distinct computation) → single `app.js` at `pages/<slug>/`. No
`scripts/` subdirectory.

### When to create `scripts/`

When the page has **two or more** of the following:
1. Shared mutable state referenced by multiple functions
2. Pure computation logic with no DOM access
3. `app.js` growing past ~300 lines

### File responsibilities

| File | Role | Forbidden |
|------|------|-----------|
| `state.js` | Shared mutable model (data tree, counters, user preferences) | No DOM access, no business logic |
| `engine.js` | Pure computation: calculations, transforms, aggregations | No DOM access, no state mutation |
| `app.js` | UI orchestration: event binding, engine calls, DOM rendering | No business logic, no state definition |

### Load order in `index.html`

```html
<script src="./scripts/engine.min.js"></script>   <!-- built artifact -->
<script src="./scripts/state.js"></script>
<script src="./scripts/app.js"></script>
```

### Refactor signals

- `app.js` > 400 lines and no `engine.js` → extract business logic into `engine.js`
- Function in `app.js` with no `document.` call → candidate for `engine.js`
- `state.js` contains `document.getElementById` → move those calls to `app.js`

---

## Minification — `engine.js` → `engine.min.js`

### Why

`engine.js` is the largest JS file (pure logic, data-heavy). Minifying it reduces payload and
optionally obfuscates the business logic before distribution.

### When to minify

Before any commit that changes `engine.js`. Not required for `state.js` or `app.js` (smaller,
UI-bound, no logic to protect).

### How

```bash
npx terser scripts/engine.js --compress --mangle --output scripts/engine.min.js
```

### The naming trap — source committed, but not served

Committing `engine.js` and `engine.min.js` side by side trivially reveals the source path: the
`.min` suffix points directly to `engine.js` by convention, and any user who guesses the URL can
fetch the unminified source.

`engine.js` must stay in git (it is the source of truth, version-controlled, reviewable in MRs). The
fix is to exclude it from the deployed/served output, not from git.

**Correct pattern:**

```
scripts/
├── engine.js       ← committed — source, version-controlled
├── engine.min.js   ← committed — built artifact, served on CDN
├── state.js
└── app.js
```

Add the source file to your static host's deploy-exclusion mechanism (e.g. `.cfignore` for
Cloudflare Pages, same syntax as `.gitignore`):

```
# JS sources — served via .min.js only; source must not be reachable on the CDN
src/pages/*/scripts/engine.js
```

With that exclusion in place, the host does not upload `engine.js` to the CDN. The file is in git
(for development and MR review) but never reachable at a public URL.

**Enforce the sync with a pre-commit hook**: if `engine.js` is staged, check that `engine.min.js` is
also staged and was regenerated in the same working session. A stale `engine.min.js` should block
the commit, e.g.:

```
engine.js staged but engine.min.js not updated.
Run: npx terser scripts/engine.js --compress --mangle --output scripts/engine.min.js
Then: git add scripts/engine.min.js
```

---

## HTML — baseline rules

- No inline `<script>` blocks in `index.html` — logic goes in `.js`, styles in `.css`
- One `<h1>` per page
- Non-empty `alt` on all non-decorative `<img>`
- Load `config.js` before `app.js` when the page depends on it

---

## Data files

| Extension | Usage | Rule |
|-----------|-------|------|
| `.json` | Reference data (prices, config) | Loaded via `fetch()` in `engine.js` — never inlined in `app.js` |
| `.md` | Embedded documentation | Loaded via `fetch()` if displayed at runtime, otherwise ignored |

---

## Pre-commit checklist

1. `app.css` does not redeclare any variable from `tokens.css`
2. `<link rel="stylesheet" href="../../style.css">` comes before `app.css`
3. No hardcoded brand color in `app.css` — use `var(--accent)`
4. `state.js` contains no `document.` or `querySelector` call
5. `engine.js` contains no `document.` call and does not mutate `state.js` globals
6. `app.js` does not compute — it delegates to `engine.js`
7. `engine.min.js` is staged alongside `engine.js` — a sync-check hook should block otherwise
8. The deploy-exclusion list still names every `scripts/engine.js` (source must not be reachable on
   the CDN)
