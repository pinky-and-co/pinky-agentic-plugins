# pc-pypi

Cut a `.devN` pre-release of a PyPI library — preconditions, version bump, delivery
to the release branch, then stop before the publishing tag for an explicit human go.

## Overview

The plugin ships one skill, `dev-prerelease`, backed by a deterministic reconciliation
script. The idea: the tag push is what publishes to PyPI and a PyPI version number can
never be reused, so the skill does everything reversible — fail-fast preconditions,
doc↔code reconciliation, version bump, delivery by direct push or pull request (the
repo's rule decides) — and always stops before the tag. It never publishes on its own;
final `X.Y.Z` releases are out of scope.

## Skills

### `dev-prerelease`

Cut a `.devN` pre-release of the PyPI library in the current repo — fail-fast
preconditions, version bump, delivery to the release branch (direct push or pull
request, per the repo rule), then stop before the publishing tag for an explicit human
go. Never publishes on its own. Step 0 reconciles the repo's feature doc against its
code before the bump: a deterministic scan (`scripts/reconcile.py`, stdlib only) whose
findings are real, plus an advisory model pass that never blocks the release.

```
/pc-pypi:dev-prerelease
```

## Requirements

`python3` on the `PATH` for `scripts/reconcile.py` (stdlib only). `git` throughout.
`gh` (GitHub CLI) for the release PR and the publish-run watch — the skill targets
repos whose PyPI publish runs on GitHub Actions. `rg` (ripgrep) and `curl` for the
version checks and the PyPI verification. The target repo must be wired for
tag-driven dev publishing: a `.github/workflows/publish.yml` triggering on `v*.dev*`
and a `pyproject.toml` carrying a PEP 440 `X.Y.Z.devN` version. The optional model
pass runs through the pc-models `offline`/`panel` MCP tools; no reachable transport
skips it without blocking the release.

## Dependencies

`pc-core`.
