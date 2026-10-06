---
name: dev-prerelease
description: Cut a `.devN` pre-release of the PyPI library in the current repo — fail-fast preconditions, version bump, commit, push, then stop before the publishing tag for an explicit human go. Never publishes on its own. Use when the user asks for a dev pre-release of the library.
argument-hint: "[<library-checkout-path>] — defaults to the current directory's git repo"
allowed-tools: Bash(git branch:*), Bash(git -C * branch*), Bash(rtk git -C * branch*), Bash(rtk git branch:*), Bash(git status:*), Bash(git -C * status*), Bash(rtk git -C * status*), Bash(rtk git status:*), Bash(git fetch:*), Bash(git -C * fetch*), Bash(rtk git -C * fetch*), Bash(rtk git fetch:*), Bash(git rev-parse:*), Bash(git -C * rev-parse*), Bash(git symbolic-ref:*), Bash(git -C * symbolic-ref*), Bash(git log:*), Bash(git -C * log*), Bash(rtk git -C * log*), Bash(rtk git log:*), Bash(git add:*), Bash(git -C * add*), Bash(rtk git -C * add*), Bash(rtk git add:*), Bash(git commit:*), Bash(git -C * commit*), Bash(rtk git -C * commit*), Bash(rtk git commit:*), Bash(git push:*), Bash(git -C * push*), Bash(rtk git -C * push*), Bash(rtk git push:*), Bash(git tag:*), Bash(git -C * tag*), Bash(gh run list:*), Bash(rtk gh run list:*), Bash(gh run watch:*), Bash(rtk gh run watch:*), Bash(rg:*), Bash(rtk rg:*), Bash(cat:*), Bash(rtk read:*), Bash(curl:*), Bash(rtk curl:*), Bash(python3:*), Read, Edit, AskUserQuestion, mcp__plugin_pc-core_pc-core-models__offline, mcp__plugin_pc-core_pc-core-models__panel
---

# Dev pre-release (PyPI via tag)

Cut a `.devN` pre-release of the library in the current repo: bump the version, commit, push the
release branch, then **stop before the tag**. The tag push is what publishes to PyPI
(irreversible), so it always needs an explicit human go — this skill never publishes on its own.

**Scope: dev pre-releases only.** Final `X.Y.Z` releases go a different way (mainline tag, changelog,
release notes — guarded separately) — out of scope here. If the current version is already a final
`X.Y.Z`, stop and say so.

**Applicability — check first, stop with the reason if it does not hold.** This skill fits a repo
whose PyPI publish is driven by a version tag: a `.github/workflows/publish.yml` that triggers on
`v*.dev*` and a `pyproject.toml` carrying a PEP 440 `X.Y.Z.devN` version. A repo missing either is
not wired for tag-driven dev publishing — say which piece is absent and stop.

## Target repo — resolve, then confirm

There is no repo argument by default: this acts on **one** library checkout, the one you point it
at. Resolve it before anything else:

- An argument was given → treat it as the checkout path; `cd` there.
- No argument → the repo is `git rev-parse --show-toplevel` from the current directory.
- That command fails (not inside a git repo), or resolves to a multi-repo workspace root rather
  than a single library → **stop** and ask the user to run this from inside the target library's
  checkout (or pass its path).

Announce the resolved repo — its directory name and the `[project].name` from its `pyproject.toml`
— and do not write anything until that is confirmed right.

## Release branch — resolve, never assume

The branch a pre-release is tagged from is a per-repo choice, never hardcoded here. Read
`branch_workflow` from the nearest `.claude/settings.pinky.json` (walk up from the repo root):

- `git flow` → the integration branch `develop`
- `github flow` (or any trunk-based value) → the trunk `main`
- key or file absent → infer from `git symbolic-ref refs/remotes/origin/HEAD` and **say which
  branch you picked and why** before continuing — do not guess silently

Call the resolved name `<release-branch>` in every step below.

## Preconditions — fail fast

Collect and check, reporting the exact failure and stopping if any does not hold. Do not silently
fix a failed precondition — report it and let the user decide.

- `git branch --show-current` → must be `<release-branch>` (resolved above).
- `git status --porcelain` → must be empty. A dirty tree is uncommitted work — never release it.
- `git fetch origin` then `git rev-parse @` vs `git rev-parse @{u}` → the local branch must not be
  behind its upstream.
- `rg '^version' pyproject.toml` → the current version MUST match `X.Y.Z.devN`. A final `X.Y.Z`
  means this skill does not apply (see scope above).
- `test -f .github/workflows/publish.yml` and confirm it triggers on `v*.dev*` — if the repo is not
  wired for dev PyPI publishing, stop.

## Step 0 — Doc reconciliation (advisory, never blocks)

Releasing a package whose feature documentation contradicts the code ships a lie, so a reconciliation
of the feature doc against the code belongs here, before the bump. Two layers: a **deterministic**
pass whose findings are real, and a **model** pass that is **advisory** — it is a lead generator, not
a gate, and a missing model transport skips it without blocking the release.

`${CLAUDE_SKILL_DIR}/scripts/reconcile.py` — call it `$RECONCILE` below — drives every step
(stdlib only, no model calls). Resolve two per-repo values first, from the nearest
`.claude/settings.pinky.json` (walk up from the repo root), falling back to the defaults:

- `reconcile.features_doc` → the capability table's repo-relative path (default
  `docs/explanation/2-features.md`)
- `reconcile.features_header` → its 3 column headers, lowercased, comma-separated (default
  `feature,module,status`)

Pass both as `--features-doc <path> --features-header <a,b,c>` on every `$RECONCILE` call below.

### 0a — Deterministic scan

```
python3 "$RECONCILE" --repo <repo> --features-doc <path> --features-header <a,b,c>
```

- **Exit 2** → the reconcile does not apply (no such doc, or its table shape is unrecognised).
  Report the reason it printed and go to Step 1. This is not a failure.
- **Exit 0 or 1** → it prints deterministic divergences and a residue count. The divergences
  (`stable-absent`, `planned-present`, `module-missing`) are **real** — show them and have the user
  fix the doc or the code before continuing. The residue rows are claims determinism cannot refute;
  the model pass judges those.

### 0b — Model pass (advisory)

1. Build the prompt:
   ```
   python3 "$RECONCILE" --repo <repo> --features-doc <path> --features-header <a,b,c> --emit-prompt
   ```
2. Run it, in this order, stopping at the first that answers:
   - `offline` (MCP tool `mcp__plugin_pc-core_pc-core-models__offline`) with `task` = the emitted
     prompt. It returns a status line and a local file path (never the answer). `Read` that file —
     you are allowed to: the prompt carries only the feature doc and public signatures, no private
     bodies. A `[offline] BLOCKED` line means MLX-SERVE unreachable, out of memory, or `pi` missing
     — fall through.
   - `panel` (MCP tool `mcp__plugin_pc-core_pc-core-models__panel`) with `tasks` = `[the prompt]`
     and `models` = `["mistral-medium-3.5"]`. Pin that model — it is the one cloud pool entry the
     reconcile oracle passes; the unpinned default does not. A `[panel] BLOCKED` line means no key —
     fall through.
   - Neither answered → print "reconcile — modèle non joignable, passe advisory sautée" and go to
     Step 1. **Never block the release on this.**
3. Filter the model's MANQUANT findings through the deterministic post-filter (kills the recurring
   "add a row for something already in the table" false positive):
   ```
   printf '%s' "<the model's full reply>" | python3 "$RECONCILE" --repo <repo> \
     --features-doc <path> --features-header <a,b,c> --filter-model-output
   ```
   Its stdout is the MANQUANT findings that survived, one per line (empty = all were noise).
4. Present, for accept/reject: the model's **DIVERGE** findings (read from its prose report) and the
   **surviving MANQUANT** findings. For each one the user accepts, `Edit` **exactly that one line**
   of the feature doc — nothing else. Reject-all → the doc is left byte-for-byte unchanged.

**Vigilance — the model pass is advisory, treat every finding as a lead to verify, not a verdict.**
Known failure modes: a small model rubber-stamps a thin brief, and cloud models vary (only local
`gemma4:12b` and `mistral-medium-3.5` clear the oracle). On a repo's first few releases, read the
feature doc against the code by hand regardless of what this step reports.

The `adversarial` MCP tool is **never** used here: it injects a review methodology and a contrarian
posture built for auditing this session's own work, which is wrong for a neutral doc↔code diff.

Then proceed to Step 1.

## Step 1 — Bump + prep (reversible — do it)

1. Compute the next version by incrementing `devN` (e.g. `0.1.0.dev2` → `0.1.0.dev3`).
2. Edit `pyproject.toml`'s `version = "…"` to the new value.
3. Commit with the `🔖 release` gitmoji — the message is fully specified here, no convention lookup
   needed: `git add pyproject.toml && git commit -m "🔖 release: v<new>"`. Never `--no-verify` — the
   hooks (version-format, etc.) are part of the gate.
4. `git push origin <release-branch>`.

Report: `<old> → <new>` and the pushed commit sha.

## Step 2 — Confirmation gate ⛔ (the outward step)

**STOP.** Show the exact command that will publish, verbatim:

```
git tag v<new> && git push origin v<new>   # → publish.yml → PyPI (irreversible)
```

State plainly: this publishes `<lib> v<new>` to PyPI, and **a PyPI version number can never be
reused**. Wait for an explicit go. Do not tag until the user confirms.

## Step 3 — Tag + publish (only after the go)

1. `git tag v<new> && git push origin v<new>`.
2. Watch the publish run: `gh run list --workflow=publish.yml --limit 1` to get the run id, then
   `gh run watch <id> --exit-status`.
3. Verify on PyPI: `curl -s https://pypi.org/pypi/<lib>/json` and confirm `<new>` appears in
   `releases` (`<lib>` = the repo's `[project].name` in `pyproject.toml`).
4. Report: published ✅ (version + PyPI confirmation), or the failure with the run URL. If the run
   fails **before** the upload step, the version number is still free — fix and re-tag; if it fails
   **during/after** upload, the number is spent — bump to the next dev.
