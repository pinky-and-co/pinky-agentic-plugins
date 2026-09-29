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
    if [ "$REPO_PATH_SET" = "yes" ]; then
        printf '%s\n' "$REPO_PATH"
        return 0
    fi
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

# Vrai si `$1` figure parmi les arguments suivants (comparaison littérale).
in_list() {
    local needle="$1" item
    shift
    for item in "$@"; do [ "$item" = "$needle" ] && return 0; done
    return 1
}

# Joint les arguments 2..n avec le séparateur `$1`.
join_by() {
    local sep="$1" out="$2" item
    shift 2
    for item in "$@"; do out="$out$sep$item"; done
    printf '%s' "$out"
}

# Avance la branche de base locale jusqu'à `origin/<base>`, en fast-forward uniquement, et imprime
# le nombre de commits gagnés (rien si zéro ou impossible). Checkoutée : `merge --ff-only`, qui
# refuse de lui-même d'écraser une modification locale en conflit. Sinon : `fetch .` met la ref à
# jour sans toucher HEAD ni l'arbre de travail, et refuse tout ce qui n'est pas un fast-forward.
update_base() {
    local repo="$1" base="$2" head="$3" before after
    before="$(git -C "$repo" rev-parse --verify --quiet "refs/heads/$base")" || return 0
    if [ "$head" = "$base" ]; then
        git -C "$repo" merge --ff-only "origin/$base" >/dev/null 2>&1 || return 0
    else
        git -C "$repo" fetch . "origin/$base:$base" >/dev/null 2>&1 || return 0
    fi
    after="$(git -C "$repo" rev-parse "refs/heads/$base")"
    [ "$before" = "$after" ] || git -C "$repo" rev-list --count "$before..$after"
}

# ── un repo ───────────────────────────────────────────────────────────────────
clean_repo() {
    local repo="$1" name base head wt
    name="$(basename "$repo")"

    # Pas de remote `origin` : rien ne peut y être [gone] ni en avance — silence, pas un échec.
    git -C "$repo" remote get-url origin >/dev/null 2>&1 \
        || { NOTHING_TOTAL=$((NOTHING_TOTAL + 1)); return 0; }

    git -C "$repo" fetch --prune origin >/dev/null 2>&1 \
        || { printf '  ❌ %-28s fetch --prune failed\n' "$name"; FAILED_TOTAL=$((FAILED_TOTAL + 1)); return 1; }

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

    # Une branche poussée sans `-u` n'a pas d'upstream : `branch -vv` ne l'affichera jamais
    # `: gone]`. Elle compte comme [gone] si `origin/<nom>` a disparu ET que sa pointe est le
    # second parent d'un merge de `origin/<base>` — la trace exacte d'une PR fusionnée. Être
    # simple ancêtre de la base ne suffit pas : une branche fraîchement créée, sans commit, l'est.
    base="$(resolve_base "$repo")"
    local merged_tips
    merged_tips="$(git -C "$repo" log --merges --first-parent --format='%P' "origin/$base" 2>/dev/null \
        | awk '{print $2}')"
    local branch upstream tip
    while IFS=' ' read -r branch upstream; do
        [ -z "$upstream" ] && [ "$branch" != "$base" ] || continue
        git -C "$repo" show-ref --verify --quiet "refs/remotes/origin/$branch" && continue
        tip="$(git -C "$repo" rev-parse "refs/heads/$branch")"
        printf '%s\n' "$merged_tips" | grep -qxF "$tip" && gone+=("$branch")
    done < <(git -C "$repo" for-each-ref --format='%(refname:short) %(upstream:short)' refs/heads)

    head="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || true)"

    # `branch -D` refuse sur la branche courante : bascule sur la base seulement si HEAD est
    # lui-même [gone]. Une branche vivante reste checkoutée — le hook tourne au démarrage d'une
    # session, l'en déloger casserait le travail en cours.
    local -a report=()
    if [ "$head" != "$base" ] && in_list "$head" ${gone[@]+"${gone[@]}"}; then
        git -C "$repo" checkout "$base" >/dev/null 2>&1 \
            || { printf '  ❌ %-28s cannot check out %s\n' "$name" "$base"; \
                 FAILED_TOTAL=$((FAILED_TOTAL + 1)); return 1; }
        report+=("HEAD $head → $base")
        head="$base"
    fi

    local pulled
    pulled="$(update_base "$repo" "$base" "$head")"
    [ -z "$pulled" ] || report+=("$base +$pulled commit(s)")

    local -a removed=()
    local failed=0
    for branch in ${gone[@]+"${gone[@]}"}; do
        [ "$branch" != "$base" ] || continue
        wt="$(worktree_for_branch "$repo" "$branch")"
        [ -z "$wt" ] || git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || true
        if git -C "$repo" branch -D "$branch" >/dev/null 2>&1; then
            removed+=("$branch")
        else
            failed=$((failed + 1))
        fi
    done

    BRANCHES_TOTAL=$((BRANCHES_TOTAL + ${#removed[@]}))
    [ ${#removed[@]} -eq 0 ] || report+=("removed: $(join_by ', ' "${removed[@]}")")

    if [ "$failed" -gt 0 ]; then
        printf '  ❌ %-28s %d branch deletion(s) failed (see git branch -vv)\n' "$name" "$failed"
        FAILED_TOTAL=$((FAILED_TOTAL + 1))
    elif [ ${#report[@]} -gt 0 ]; then
        printf '  ✅ %-28s %s\n' "$name" "$(join_by ' · ' "${report[@]}")"
        OK_TOTAL=$((OK_TOTAL + 1))
    else
        [ "$QUIET" = true ] || printf '  •  %-28s nothing to clean\n' "$name"
        NOTHING_TOTAL=$((NOTHING_TOTAL + 1))
    fi
}

# ── CLI ───────────────────────────────────────────────────────────────────────
ROOT="$PWD"; ROOT_SET="no"; REPO_PATH_SET="no"; REPO_PATH=""; FILTER=""; QUIET=false
while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT="$2"; ROOT_SET="yes"; shift 2 ;;
        --repo-path) [ $# -ge 2 ] || die "--repo-path needs a value"; REPO_PATH="$2"; REPO_PATH_SET="yes"; shift 2 ;;
        --repo) FILTER="$2"; shift 2 ;;
        --all)  shift ;;  # no-op : absence de --repo fait déjà tout le root
        --quiet) QUIET=true; shift ;;  # Supprime la sortie per-repo
        *)      die "unknown option: $1" ;;
    esac
done

# `--repo-path` désigne EXACTEMENT un repo : chemin absolu, dossier existant portant un `.git`
# (fichier ou dossier — un worktree compte), sans `..`. Incompatible avec `--root`/`--repo`.
if [ "$REPO_PATH_SET" = "yes" ]; then
    [ "$ROOT_SET" = "no" ] && [ -z "$FILTER" ] || die "--repo-path cannot be combined with --root or --repo"
    case "$REPO_PATH" in
        /*) ;;
        *) die "--repo-path must be an absolute path: $REPO_PATH" ;;
    esac
    case "/$REPO_PATH/" in
        */../*) die "--repo-path must not contain '..': $REPO_PATH" ;;
    esac
    [ -d "$REPO_PATH" ] && [ -e "$REPO_PATH/.git" ] || die "--repo-path is not a git repository: $REPO_PATH"
fi

scanned=0
OK_TOTAL=0; NOTHING_TOTAL=0; FAILED_TOTAL=0; BRANCHES_TOTAL=0
while IFS= read -r repo; do
    [ -n "$repo" ] || continue
    name="$(basename "$repo")"
    [ -z "$FILTER" ] || [ "$name" = "$FILTER" ] || continue
    scanned=$((scanned + 1))
    clean_repo "$repo" || true
done < <(discover_repos "$ROOT")

[ "$scanned" -gt 0 ] || die "no git repository found under $ROOT (or --repo $FILTER matched nothing)"

[ "$QUIET" = true ] || printf '\nsummary: %d repo(s) cleaned · %d nothing to clean · %d failed · %d branch(es) removed\n' \
    "$OK_TOTAL" "$NOTHING_TOTAL" "$FAILED_TOTAL" "$BRANCHES_TOTAL"
[ "$FAILED_TOTAL" -eq 0 ]
