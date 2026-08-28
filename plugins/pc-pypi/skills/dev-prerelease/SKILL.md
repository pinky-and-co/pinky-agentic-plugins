---
name: dev-prerelease
description: Cut a `.devN` pre-release of the PyPI library in the current repo — fail-fast preconditions, version bump, commit, push, then stop before the publishing tag for an explicit human go. Never publishes on its own.
argument-hint: "[<library-checkout-path>] — defaults to the current directory's git repo"
allowed-tools: Bash(git branch:*), Bash(git status:*), Bash(git fetch:*), Bash(git rev-parse:*), Bash(git symbolic-ref:*), Bash(git log:*), Bash(git add:*), Bash(git commit:*), Bash(git push:*), Bash(git tag:*), Bash(gh run list:*), Bash(gh run watch:*), Bash(rg:*), Bash(cat:*), Bash(curl:*), Read, Edit, AskUserQuestion
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

## Step 0 — Doc reconciliation gate

Releasing a package whose feature documentation contradicts the code ships a lie, so a reconciliation
of the feature doc against the code belongs here, before the bump.

**Not implemented in this skill yet.** Until it is: say so plainly, and have the user verify by hand
that the repo's feature documentation still matches the code before continuing. Never block the
release on this step.

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
