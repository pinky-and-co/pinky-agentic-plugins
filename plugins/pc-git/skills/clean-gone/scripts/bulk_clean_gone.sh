#!/usr/bin/env bash
# bulk_clean_gone.sh — supprime les branches locales [gone] sur un ou tous les repos du workspace.
#
# `--repo <name>` filtre à un seul repo (basename match) ; sans filtre, tout repo trouvé sous
# `--root` est traité — même forme que bulk_commit.sh (hooks/branch-policy), symétrie voulue.
#
# fetch --prune passe AVANT toute lecture de `[gone]` : une branche supprimée côté serveur
# (delete_branch_on_merge, merge GitHub) reste invisible dans les refs locales tant que rien ne
# prune — sans ce fetch, "rien à nettoyer" est structurellement incapable de voir le cas qu'il
# existe pour détecter.

set -euo pipefail

die() { printf 'clean-gone: %s\n' "$1" >&2; exit 1; }

discover_repos() {
    local root="$1"
    find "$root" -maxdepth 4 -name .git -not -path '*/node_modules/*' -print 2>/dev/null \
        | while IFS= read -r g; do dirname "$g"; done | sort
}

# Même calcul que resolve_base() de bulk_commit.sh (hooks/branch-policy) : la branche
# d'intégration vient de origin/HEAD, jamais du HEAD courant (qui peut être une feature branch
# au moment de l'appel) — cas le plus fréquent ici : HEAD est justement la branche [gone] qu'on
# s'apprête à supprimer, la retenir comme base la rendrait indélogeable (checkout et suppression
# tous deux sautés silencieusement, cf. `[ "$head" != "$base" ]` et `[ "$branch" != "$base" ]`).
resolve_base() {
    local repo="$1" ref cand
    ref="$(git -C "$repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    if [ -n "$ref" ]; then
        printf '%s' "${ref#origin/}"
        return
    fi
    # origin/HEAD non résolu (repo jamais `clone`, remote HEAD jamais fixé) : main/master avant
    # HEAD, sinon le cas ci-dessus se reproduit dès que HEAD est la branche à supprimer.
    for cand in main master; do
        git -C "$repo" show-ref --verify --quiet "refs/heads/$cand" \
            && { printf '%s' "$cand"; return; }
    done
    git -C "$repo" symbolic-ref --short HEAD
}

# Chemin du worktree associé à une branche, vide si aucun. `git worktree list --porcelain` émet
# un bloc `worktree <path>` suivi de `branch refs/heads/<name>` — la ligne worktree précède
# toujours la ligne branch dans le même bloc, d'où le suivi séquentiel plutôt qu'un parse par bloc.
worktree_for_branch() {
    local repo="$1" branch="$2" wt=""
    while IFS= read -r line; do
        case "$line" in
            worktree\ *) wt="${line#worktree }" ;;
            # Comparaison littérale, jamais un pattern `case` : un nom de branche avec `*`, `?`
            # ou `[` casserait ou faux-matcherait un glob.
            branch\ *)
                [ "${line#branch }" = "refs/heads/$branch" ] && { printf '%s' "$wt"; return; } ;;
        esac
    done < <(git -C "$repo" worktree list --porcelain 2>/dev/null)
}

# ── un repo ───────────────────────────────────────────────────────────────────
clean_repo() {
    local repo="$1" name base head wt
    name="$(basename "$repo")"

    git -C "$repo" fetch --prune origin >/dev/null 2>&1 \
        || { printf '  ❌ %-28s fetch --prune échoué\n' "$name"; FAILED_TOTAL=$((FAILED_TOTAL + 1)); return 1; }

    local -a gone=()
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            # Format réel de `branch -vv` : `[origin/x: gone]`, jamais `[gone]` isolé — matcher
            # sur `[gone]` seul ne matche rien. HEAD courant s'affiche `* nom …` : le préfixe
            # s'ôte AVANT de prendre le 1er champ, jamais après (`$1` vaudrait `*`, pas le nom).
            *': gone]'*)
                gone+=("$(printf '%s' "$line" | sed 's/^[*+ ]*//' | awk '{print $1}')") ;;
        esac
    done < <(git -C "$repo" branch -vv)

    if [ ${#gone[@]} -eq 0 ]; then
        printf '  •  %-28s rien à nettoyer\n' "$name"
        NOTHING_TOTAL=$((NOTHING_TOTAL + 1))
        return 0
    fi

    base="$(resolve_base "$repo")"
    head="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || true)"

    # `branch -D` refuse sur la branche courante : bascule sur la base avant toute suppression,
    # que HEAD soit une branche [gone] elle-même ou une autre branche non concernée.
    if [ "$head" != "$base" ]; then
        git -C "$repo" checkout "$base" >/dev/null 2>&1 \
            || { printf '  ❌ %-28s checkout %s impossible\n' "$name" "$base"; \
                 FAILED_TOTAL=$((FAILED_TOTAL + 1)); return 1; }
    fi
    # Toujours après bascule, même si HEAD y était déjà : un HEAD resté sur la base depuis avant
    # le merge qui vient de justifier ce nettoyage est tout aussi en retard.
    git -C "$repo" pull --ff-only >/dev/null 2>&1 || true

    local branch removed=0 failed=0
    for branch in "${gone[@]}"; do
        [ "$branch" != "$base" ] || continue
        wt="$(worktree_for_branch "$repo" "$branch")"
        [ -z "$wt" ] || git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || true
        if git -C "$repo" branch -D "$branch" >/dev/null 2>&1; then
            removed=$((removed + 1))
        else
            failed=$((failed + 1))
        fi
    done

    if [ "$failed" -gt 0 ]; then
        [ "$QUIET" = true ] || printf '  ❌ %-28s %d supprimée(s), %d échec(s) (voir git branch -vv)\n' \
            "$name" "$removed" "$failed"
        FAILED_TOTAL=$((FAILED_TOTAL + 1))
    elif [ "$removed" -gt 0 ]; then
        [ "$QUIET" = true ] || printf '  ✅ %-28s %d branche(s) [gone] supprimée(s)\n' "$name" "$removed"
        OK_TOTAL=$((OK_TOTAL + 1))
    else
        [ "$QUIET" = true ] || printf '  •  %-28s rien à nettoyer\n' "$name"
        NOTHING_TOTAL=$((NOTHING_TOTAL + 1))
    fi
}

# ── CLI ───────────────────────────────────────────────────────────────────────
ROOT="$PWD"; FILTER=""; QUIET=false
while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT="$2"; shift 2 ;;
        --repo) FILTER="$2"; shift 2 ;;
        --all)  shift ;;  # no-op : absence de --repo fait déjà tout le root
        --quiet) QUIET=true; shift ;;  # Supprime la sortie per-repo
        *)      die "option inconnue : $1" ;;
    esac
done

scanned=0
OK_TOTAL=0; NOTHING_TOTAL=0; FAILED_TOTAL=0
while IFS= read -r repo; do
    [ -n "$repo" ] || continue
    name="$(basename "$repo")"
    [ -z "$FILTER" ] || [ "$name" = "$FILTER" ] || continue
    scanned=$((scanned + 1))
    clean_repo "$repo" || true
done < <(discover_repos "$ROOT")

[ "$scanned" -gt 0 ] || die "aucun dépôt git trouvé sous $ROOT (ou filtre --repo $FILTER sans correspondance)"

[ "$QUIET" = true ] || printf '\n✅ %d   •  %d   ❌ %d\n' "$OK_TOTAL" "$NOTHING_TOTAL" "$FAILED_TOTAL"
[ "$FAILED_TOTAL" -eq 0 ]
