#!/usr/bin/env bash
# branch_paths.sh — single source of truth for which paths skip the branch guard.
#
# Two callers needed the same "is this path exempt from the feature-branch requirement"
# check — bulk_commit.sh (bash, staged-diff-driven) and branch_guard.py (a single file per
# PreToolUse call), both siblings in this branch-policy/ folder. Duplicating the glob list in
# both was flagged as a certain future divergence (deux-hooks-branche-et-cout-de-lecture,
# 2026-08-19); this file is the fix — sourced by bulk_commit.sh as a function, shelled out to
# by branch_guard.py.
#
# CLI mode (non-bash callers): branch_paths.sh <repo-root-relative-path>
#   exit 0 = unguarded (direct commit allowed), exit 1 = guarded (needs a feature branch).
# Library mode (bash callers): source this file, then call is_unguarded "<path>".

# `uv.lock` accepted here sciemment (2026-08-19, décision user) : fichier ~2000 lignes, jamais
# écrit à la main, régénéré uniquement par `uv lock`/`uv sync` — de nature équivalente à un
# artefact local, pas à du code source portant une décision.
#
# Une PR n'aurait de toute façon protégé contre rien ici : un numéro de version compromis (ex.
# `httpx==0.28.1` → `0.28.3` malveillant) est indétectable à l'œil dans un diff de lockfile —
# ça prend de l'outillage dédié (scan de vulnérabilités, vérification de hash), jamais une revue
# humaine. Et GitHub replie déjà ce diff par défaut ("generated file"), donc même les PR passées
# avant cette décision n'étaient quasi jamais lues. Retirer l'exigence de branche ne sacrifie
# aucune protection réelle — il n'y en avait pas à ce niveau.
UNGUARDED_GLOBS=(
    'docs/*' '*.md' '.github/*' 'core/rules/*' '.claude/*'
    'pyproject.toml' '.pre-commit-config.yaml' 'profile.yaml' '.darglint'
    '.gitignore' '.editorconfig' 'scripts/hooks/*' '.gitleaks.toml' 'uv.lock'
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
