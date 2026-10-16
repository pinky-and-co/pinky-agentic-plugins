#!/usr/bin/env bash
# branch_paths.sh — single source of truth for which paths skip the branch guard.
#
# Shared by both callers so the glob list can't diverge between them: sourced by bulk_commit.sh
# as a function, shelled out to by branch_guard.py.
#
# CLI mode (non-bash callers): branch_paths.sh <repo-root-relative-path>
#   exit 0 = unguarded (direct commit allowed), exit 1 = guarded (needs a feature branch).
# Library mode (bash callers): source this file, then call is_unguarded "<path>".

# Only named files and anchored documentation paths are exempt — never a blanket `*.md`, and no
# `.*/*` glob over hidden directories (`.github/` and `.claude/` are the two named exceptions): a
# markdown file can be a rule source that drives agent behavior (e.g. agentic-harness/rules/*.md,
# a SKILL.md), not just documentation. A plugin's `plugin.json` and the marketplace manifest are
# named one by one, never through a `.claude-plugin/*` glob: besides descriptions they also carry
# `dependencies` and `version`, which change what gets installed.
#
# uv.lock stays unguarded: a ~2000-line generated file, never hand-written, regenerated only by
# `uv lock`/`uv sync` — equivalent to a local artifact, not source code carrying a decision. A PR
# wouldn't protect anything here either: a compromised pinned version (e.g. a malicious
# `httpx==0.28.3`) is invisible in a lockfile diff by eye — that takes dedicated tooling
# (vulnerability scan, hash check), never a human review, and GitHub collapses this diff by default
# ("generated file") anyway. Dropping the branch requirement sacrifices no real protection.
UNGUARDED_GLOBS=(
    'docs/*' 'README.md' 'AGENTS.md' 'CLAUDE.md' 'LICENSE' '.github/*' '.claude/*'
    'pyproject.toml' '.pre-commit-config.yaml' '.darglint'
    '.gitignore' '.editorconfig' 'scripts/hooks/*' '.gitleaks.toml' 'uv.lock'
    'plugins/*/README.md' 'plugins/*/docs/*'
    '.claude-plugin/marketplace.json' 'plugins/*/.claude-plugin/plugin.json'
)

is_unguarded() {
    local f="$1" g
    for g in "${UNGUARDED_GLOBS[@]}"; do
        # shellcheck disable=SC2254
        case "$f" in $g) return 0 ;; esac
    done
    return 1
}

# Executed directly (not sourced) → CLI mode, for the python hook.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    is_unguarded "$1"
    exit $?
fi
