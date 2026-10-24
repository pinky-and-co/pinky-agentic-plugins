# pc-delegate

Export/reintegrate workflow for delegating work to an agent operating outside Claude Code — export context, reintegrate its results safely.

## Overview

pc-delegate ships two skills, `export` and `reintegrate`, plus one MCP tool that computes the code subtree an export carries. The idea: a web-based external agent (Cortex/Snowsight, claude.ai web, any agent outside Claude Code) has no local git and no access to the repo, so the context it needs — selected kanban cards, chantier context, a relevance-filtered `_AGENT_LOG`, the repo code subtree, and a rendered working guide — is packed into one zip. What comes back is applied under guards (no silent overwrite, no path escape), verified with the repo's own tooling, reviewed, shipped as a PR, and the covered cards are archived.

## Skills

### `export`

Exports selected kanban cards with their chantier context, a relevance-filtered `_AGENT_LOG`, a repo code subtree and a rendered guide into one zip for an agent working outside Claude Code; the exported cards move to `3-in-progress` with an `exported:` marker. `--dry-run` previews which cards would move and how many files would be offered without writing anything. The real run applies the secrets denylist to every repo file, ships only the `_AGENT_LOG` entries relevant to the exported cards, and prints a ready-to-paste prompt for the external agent's chat.

```
"Export cards 0231 and 0234 of chantier 0007 to hand off to an external agent"
```

### `reintegrate`

Reintegrates what an external agent produced — a zip or directory of created or modified files. Application is safe by construction: a locally-diverged file is refused, never overwritten; a `..` or absolute entry is refused outright; `.py` files are classified comments-only vs code-changed; the repo's own verify loop runs, the diff is reviewed, the PR is opened through `commit-and-pr`, and the covered cards — listed in the return's `EXPORTED_CARDS.txt` — are archived. A `REPORT.md` at the root of the return is pulled out as a `_AGENT_LOG` entry (or printed), never applied into the repo.

```
"Reintegrate this zip"
```

## MCP tools

- `export_subtree` — returns the files the export leg should zip for a repo: every touched file plus the modules each one directly imports (depth 1, never transitive), resolved on disk via `grimp` for a proper Python package or an AST scan for a standalone script. One repo-relative path per line, sorted, de-duplicated — never the file contents.

## Requirements

- `python3` — runs every skill script (`build_export.py`, `secrets_filter.py`, `apply_reintegrate.py`, `close_cards.py`); all stdlib-only.
- `uv` — runs the MCP server (`uv run --script`, PEP 723 inline dependencies `mcp` and `grimp`; Python >= 3.12).
- `git` — the reintegrate leg reads status/diff and refuses tracked files with an uncommitted diff.
- `ruff`, `mypy`, `pytest` and `prek` or `pre-commit` — the reintegrate verify loop runs whichever of these the target repo declares.
- A pc-kanban chantier board — both legs select, mark and close cards on it.

## Dependencies

pc-core, pc-git.
