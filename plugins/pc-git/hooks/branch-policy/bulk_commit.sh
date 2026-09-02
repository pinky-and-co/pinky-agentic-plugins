#!/usr/bin/env bash
# bulk_commit.sh — le déterministe d'un commit multi-repos, sans agent par repo.
#
# Deux passes, parce que la seule chose qui demande un modèle est la rédaction du message :
#
#   plan   → scanne, stage, groupe les repos par EMPREINTE DE DIFF, émet le plan en texte.
#            L'appelant (l'agent, dans son propre tour) rédige UN message par groupe.
#   apply  → relit les messages, crée la branche s'il faut, commit, push, ouvre la PR/MR.
#
# Le groupement par empreinte est le coeur : le cas d'usage réel de --all est une
# distribution identique sur N repos, donc N repos partagent UN diff et UN message.
# 20 repos → 2 ou 3 messages à rédiger, pas 20.

set -euo pipefail

EXCLUDED_DEFAULT="node_modules,.venv,__pycache__"
NOISE_FILES=".DS_Store Thumbs.db desktop.ini"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVE_POLICY="$SCRIPT_DIR/resolve_policy.py"

# Chemins non gardés par la politique de branche : un commit qui ne touche QUE ces chemins va
# directement sur la branche d'intégration, sans feature branch. Liste + `is_unguarded()`
# mutualisées avec branch_guard.py, ce dossier (deux-hooks-branche-et-cout-de-lecture,
# 2026-08-19) — voir branch_paths.sh pour le détail glob par glob (`uv.lock` notamment).
# shellcheck source=branch_paths.sh
source "$SCRIPT_DIR/branch_paths.sh"

die() { printf 'bulk_commit: %s\n' "$1" >&2; exit 1; }

# Racine sans aucun dépôt git : périmée (workspace déplacé), pas « rien à committer ».
# Partagé par `do_discover` et `do_plan` pour que le message ne dérive pas entre les deux.
no_repos_found() {
    printf 'NO-REPOS-FOUND sous : %s\n' "$1"
    printf 'Aucun dépôt git sous cette racine — elle est probablement périmée\n'
    printf '(workspace déplacé ?). Vérifier --root avant de conclure quoi que ce soit.\n'
}

# ── Empreinte d'un diff stagé ─────────────────────────────────────────────────
# Les lignes `index abc..def` portent des SHA propres à chaque repo : deux diffs
# par ailleurs identiques ne hasheraient jamais pareil si on les gardait.
# `.gitignore` est exclu : resolve_noise l'écrit dans les seuls repos qui traînaient un
# `.DS_Store`, et sans cette exclusion ces repos sortiraient du groupe de la distribution
# qu'ils partagent avec les autres — le bruit fragmenterait le groupement.
NOISE_ONLY_FP="noise-gitignore"

fingerprint() {
    local d
    d="$(git -C "$1" diff --staged -- . ':(exclude).gitignore' | grep -v '^index ' || true)"
    [ -n "$d" ] || { printf '%s' "$NOISE_ONLY_FP"; return; }
    printf '%s' "$d" | shasum | cut -d' ' -f1 | cut -c1-12
}

# Un seul fichier gardé suffit à exiger une feature branch.
needs_feature_branch() {
    local repo="$1" f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        is_unguarded "$f" || return 0
    done < <(git -C "$repo" diff --staged --name-only)
    return 1
}

branch_strategy() {
    local repo="$1"
    [ -f "$RESOLVE_POLICY" ] || { echo "UNDECLARED"; return; }
    python3 "$RESOLVE_POLICY" branch_workflow --repo "$repo" 2>/dev/null | head -1 \
        | grep -v '^$' || echo "UNDECLARED"
}

# La branche d'intégration réelle, jamais le HEAD de départ : un HEAD déjà sur une feature
# branch (checkout fait avant d'invoquer ce script) donnerait sinon une base fausse, et
# open_pr() sauterait la PR en croyant être sur la branche d'intégration.
resolve_base() {
    local repo="$1" ref
    ref="$(git -C "$repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    if [ -n "$ref" ]; then
        printf '%s' "${ref#origin/}"
        return
    fi
    git -C "$repo" symbolic-ref --short HEAD
}

# `.DS_Store` & co ne sont pas une décision : ils vont dans .gitignore, silencieusement.
resolve_noise() {
    local repo="$1" n added=0
    for n in $NOISE_FILES; do
        git -C "$repo" ls-files --others --exclude-standard | grep -qxF "$n" || continue
        grep -qxF "$n" "$repo/.gitignore" 2>/dev/null && continue
        printf '%s\n' "$n" >> "$repo/.gitignore"
        added=1
    done
    return $((1 - added))
}

discover_repos() {
    local root="$1"
    find "$root" -maxdepth 4 -name .git -not -path '*/node_modules/*' -print 2>/dev/null \
        | while IFS= read -r g; do dirname "$g"; done | sort
}

# ── discover ──────────────────────────────────────────────────────────────────
# READ-ONLY : le nom de base de chaque repo sale, un par ligne. Sert au skill à
# résoudre le scope AVANT de lancer `plan` (qui, lui, stage). `plan` sans `--repo`
# ferait `git add -A` sur tout le workspace — l'incident 2026-09-02.
#
# Émet `NO-REPOS-FOUND` + exit non nul si AUCUN dépôt sous la racine (racine périmée,
# workspace déplacé) — distinct de « des repos, tous propres » (sortie vide, exit 0),
# même séparation que `do_plan`.
do_discover() {
    local root="$1" repo name scanned=0
    while IFS= read -r repo; do
        [ -n "$repo" ] || continue
        name="$(basename "$repo")"
        case ",$EXCLUDED_DEFAULT," in *",$name,"*) continue ;; esac
        scanned=$((scanned + 1))
        if [ -n "$(git -C "$repo" status --porcelain 2>/dev/null || true)" ]; then
            printf '%s\n' "$name"
        fi
    done < <(discover_repos "$root")

    if [ "$scanned" -eq 0 ]; then
        no_repos_found "$root"
        return 1
    fi
    return 0
}

# ── plan ──────────────────────────────────────────────────────────────────────
do_plan() {
    local root="$1" filter="$2"
    local -a repos=() fps=()
    local repo name fp
    local scanned=0

    # `plan` stage (`git add -A`) chaque repo qu'il scanne. Sans `--repo`, c'est tout le
    # workspace : rendre l'effet de bord visible plutôt que silencieux (`discover` = read-only).
    [ -n "$filter" ] || printf 'bulk_commit: note: plan non filtré — staging de tous les repos sales sous %s\n' "$root" >&2

    while IFS= read -r repo; do
        [ -n "$repo" ] || continue
        name="$(basename "$repo")"
        case ",$EXCLUDED_DEFAULT," in *",$name,"*) continue ;; esac
        [ -z "$filter" ] || [ "$name" = "$filter" ] || continue

        scanned=$((scanned + 1))
        resolve_noise "$repo" || true
        git -C "$repo" add -A 2>/dev/null || true
        git -C "$repo" diff --staged --quiet && continue

        repos+=("$repo")
        fps+=("$(fingerprint "$repo")")
    done < <(discover_repos "$root")

    if [ "$scanned" -eq 0 ]; then
        no_repos_found "$root"
        return 1
    fi
    [ ${#repos[@]} -gt 0 ] \
        || { printf 'NOTHING-TO-COMMIT (%d repo(s) scanné(s), tous propres)\n' "$scanned"; return 0; }

    # Groupes = empreintes distinctes, dans l'ordre de première apparition.
    local -a seen=()
    local i j known
    for i in "${!fps[@]}"; do
        known=0
        for j in "${seen[@]:-}"; do [ "$j" = "${fps[$i]}" ] && known=1 && break; done
        [ $known -eq 1 ] || seen+=("${fps[$i]}")
    done

    printf '# %d repo(s) à committer, %d groupe(s) de diff distinct(s)\n\n' \
        "${#repos[@]}" "${#seen[@]}"

    for fp in "${seen[@]}"; do
        local -a members=()
        for i in "${!fps[@]}"; do
            [ "${fps[$i]}" = "$fp" ] && members+=("${repos[$i]}")
        done

        printf '=== GROUP %s (%d repo(s))\n' "$fp" "${#members[@]}"
        printf 'FILES: %s\n' "$(git -C "${members[0]}" diff --staged --name-only | tr '\n' ' ')"
        printf 'REPOS:\n'
        for repo in "${members[@]}"; do
            printf '  %s\tstrategy=%s\tfeature-branch=%s\n' \
                "$repo" "$(branch_strategy "$repo")" \
                "$(needs_feature_branch "$repo" && echo yes || echo no)"
        done
        printf 'DIFF (tronqué à 120 lignes, identique pour tout le groupe) :\n'
        git -C "${members[0]}" diff --staged | head -120
        printf '=== END %s\n\n' "$fp"
    done

    printf 'Rédige UN message par groupe, puis :\n'
    printf '  bulk_commit.sh apply --messages <fichier>   # lignes "<empreinte><TAB><message>"\n'
}

# ── PR / MR ───────────────────────────────────────────────────────────────────
# Un commit direct sur la branche d'intégration n'a pas de PR à ouvrir : seul un
# passage par une feature branch en demande une.
#
# Émet UNE ligne `<verdict>\t<url|->` sur stdout, jamais de prose : do_apply en dérive
# l'état et la ligne humaine. verdict ∈ pr | pr-open | pr-na | pr-failed. C'est le
# correctif de l'incident 2026-09-02 : le silence d'open_pr sur un commit direct laissait
# l'agent forké inventer « PR created ». `|| true` partout : `gh`/`glab` échouent
# normalement (pas d'auth, quota) et `set -e` tuerait le lot sur une capture d'URL ratée.

# Dernière URL de la sortie `gh`/`glab` : `pr create` l'imprime en dernière ligne,
# `pr view` l'imprime seule. `|| true` : pas de match ≠ erreur.
_pr_url() { grep -oE 'https://[^ ]+' | tail -1 || true; }

open_pr() {
    local repo="$1" branch="$2" base="$3" msg="$4" url=""
    [ "$branch" != "$base" ] || { printf 'pr-na\t-\n'; return 0; }
    case "$(git -C "$repo" remote get-url origin 2>/dev/null || true)" in
        *gitlab*)
            command -v glab >/dev/null 2>&1 || { printf 'pr-failed\t-\n'; return 0; }
            if (cd "$repo" && glab mr view >/dev/null 2>&1); then
                url="$( (cd "$repo" && glab mr view 2>/dev/null || true) | _pr_url )"
                printf 'pr-open\t%s\n' "${url:--}"; return 0
            fi
            url="$( (cd "$repo" && glab mr create --target-branch "$base" --title "$msg" \
                --description "$msg" --yes 2>/dev/null || true) | _pr_url )"
            [ -n "$url" ] && printf 'pr\t%s\n' "$url" || printf 'pr-failed\t-\n'
            ;;
        *)
            command -v gh >/dev/null 2>&1 || { printf 'pr-failed\t-\n'; return 0; }
            if (cd "$repo" && gh pr view >/dev/null 2>&1); then
                url="$( (cd "$repo" && gh pr view --json url --jq .url 2>/dev/null || true) | _pr_url )"
                printf 'pr-open\t%s\n' "${url:--}"; return 0
            fi
            url="$( (cd "$repo" && gh pr create --base "$base" --title "$msg" \
                --body "$msg" 2>/dev/null || true) | _pr_url )"
            [ -n "$url" ] && printf 'pr\t%s\n' "$url" || printf 'pr-failed\t-\n'
            ;;
    esac
}

# ── apply ─────────────────────────────────────────────────────────────────────
do_apply() {
    local root="$1" filter="$2" msgfile="$3" dry="$4" nopr="$5"
    [ -f "$msgfile" ] || die "fichier de messages introuvable : $msgfile"

    local ok=0 failed=0 skipped=0 partial=0
    local repo name fp msg strategy base head branch slug prv state url
    local -a results=()

    # Trailer d'attribution : composé ici, jamais du system-reminder (absent d'un fork
    # commit-and-pr). `Co-Authored-By` sans version — le fork tourne `haiku` mais le
    # travail est piloté par le parent ; router le modèle réintroduirait le couplage
    # "l'agent fournit la vérité" que RESULTS supprime. `CLAUDE_CODE_BRIDGE_SESSION_ID`
    # (vérifié : hérité dans un fork avec la valeur du parent) donne l'URL de session.
    local trailer="Co-Authored-By: Claude <noreply@anthropic.com>"
    if [ -n "${CLAUDE_CODE_BRIDGE_SESSION_ID:-}" ]; then
        trailer="$trailer
Claude-Session: https://claude.ai/code/${CLAUDE_CODE_BRIDGE_SESSION_ID}"
    fi

    while IFS= read -r repo; do
        [ -n "$repo" ] || continue
        name="$(basename "$repo")"
        case ",$EXCLUDED_DEFAULT," in *",$name,"*) continue ;; esac
        [ -z "$filter" ] || [ "$name" = "$filter" ] || continue

        git -C "$repo" diff --staged --quiet && continue
        fp="$(fingerprint "$repo")"
        msg="$(grep -F "$fp" "$msgfile" 2>/dev/null | head -1 | cut -f2- || true)"
        if [ "$fp" = "$NOISE_ONLY_FP" ] && [ -z "$msg" ]; then
            # rien à rédiger : le changement est connu et identique partout
            msg="🔧 chore: ignore OS noise files"
        fi
        if [ -z "$msg" ]; then
            printf '  ⏭️  %-28s aucun message pour l’empreinte %s\n' "$name" "$fp"
            results+=("$(printf '%s\tskipped\t-' "$name")")
            skipped=$((skipped + 1)); continue
        fi

        strategy="$(branch_strategy "$repo")"
        base="$(resolve_base "$repo")"
        head="$(git -C "$repo" symbolic-ref --short HEAD)"
        branch="$head"
        if [ "$head" = "$base" ] && needs_feature_branch "$repo"; then
            slug="$(printf '%s' "$msg" | sed 's/^[^:]*: *//' \
                | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-' \
                | sed 's/^-*//; s/-*$//' | cut -c1-40)"
            branch="feature/${slug:-change}"
        fi

        if [ "$dry" = "yes" ]; then
            printf '  [dry] %-28s %s → %s : %s\n' "$name" "$strategy" "$branch" "$msg"
            ok=$((ok + 1)); continue
        fi

        # Étages séparés, et nommés dans le rapport : un push qui échoue n'est pas un
        # commit qui échoue. Un "❌ échec git" indifférencié laisse croire à une perte
        # alors que le commit est bien là.
        if [ "$branch" != "$head" ] \
           && ! git -C "$repo" checkout -b "$branch" >/dev/null 2>&1; then
            printf '  ❌ %-28s branche %s impossible (modifs stagées, rien de perdu)\n' \
                "$name" "$branch"
            results+=("$(printf '%s\tfailed\t-' "$name")")
            failed=$((failed + 1)); continue
        fi
        if ! git -C "$repo" commit -m "$msg" -m "$trailer" >/dev/null 2>&1; then
            printf '  ❌ %-28s commit refusé, hook probable (modifs stagées, rien de perdu)\n' "$name"
            results+=("$(printf '%s\tfailed\t-' "$name")")
            failed=$((failed + 1)); continue
        fi
        if ! git -C "$repo" push -u origin "$branch" >/dev/null 2>&1; then
            printf '  ⚠️  %-28s committé sur %s, push échoué (commit local conservé)\n' \
                "$name" "$branch"
            results+=("$(printf '%s\tcommit-nopush\t-' "$name")")
            partial=$((partial + 1)); continue
        fi
        printf '  ✅ %-28s %s\n' "$name" "$msg"

        if [ "$nopr" = "yes" ]; then
            state="pushed-nopr"; url="-"
        else
            # open_pr émet toujours une ligne ; le fallback couvre une régression future
            # (un `read` qui échoue sous `set -e` avorterait AVANT le bloc RESULTS).
            IFS=$'\t' read -r prv url < <(open_pr "$repo" "$branch" "$base" "$msg") \
                || { prv=pr-failed; url=-; }
            case "$prv" in
                pr)         state="pushed-pr";       printf '      PR ouverte : %s\n' "$url" ;;
                pr-open)    state="pushed-pr";       printf '      PR déjà ouverte : %s\n' "$url" ;;
                pr-na)      state="pushed-nopr";     printf '      commit direct sur %s, pas de PR\n' "$base" ;;
                *)          state="pushed-prfailed"; url="-"
                            printf '      ⚠️  PR non ouverte — à ouvrir à la main\n' ;;
            esac
        fi
        results+=("$(printf '%s\t%s\t%s' "$name" "$state" "${url:--}")")
        # `pushed-prfailed` : commité + poussé, mais une PR reste à ouvrir à la main —
        # compté comme partiel, jamais dans le ✅.
        [ "$state" = "pushed-prfailed" ] && partial=$((partial + 1)) || ok=$((ok + 1))
    done < <(discover_repos "$root")

    # ⚠️ compté à part : un commit fait mais non poussé (ou une PR à ouvrir à la main)
    # n'est ni un succès ni une perte.
    printf '\n✅ %d   ⚠️  %d   ⏭️  %d   ❌ %d\n' "$ok" "$partial" "$skipped" "$failed"

    # Bloc machine : l'agent (Step 4 du skill) le reproduit tel quel, sans en dériver une
    # phrase absente. `state` ∈ pushed-pr | pushed-nopr | pushed-prfailed | commit-nopush
    # | skipped | failed. Correctif de l'incident 2026-09-02 (résumé forké inventé).
    # Sauté en `--dry-run` : rien n'a été fait, un bloc vide serait un piège pour l'agent.
    if [ "$dry" != "yes" ]; then
        printf '\n=== RESULTS (repo\tstate\tpr_url) — reproduire tel quel\n'
        local line
        for line in "${results[@]:-}"; do [ -n "$line" ] && printf '%s\n' "$line"; done
        printf '=== END RESULTS\n'
    fi

    [ "$failed" -eq 0 ]
}

# ── CLI ───────────────────────────────────────────────────────────────────────
MODE="${1:-}"; shift || true
ROOT="$PWD"; FILTER=""; MSGFILE=""; DRY="no"; NOPR="no"
while [ $# -gt 0 ]; do
    case "$1" in
        --root)     ROOT="$2"; shift 2 ;;
        --repo)     FILTER="$2"; shift 2 ;;
        --messages) MSGFILE="$2"; shift 2 ;;
        --dry-run)  DRY="yes"; shift ;;
        --no-pr)    NOPR="yes"; shift ;;
        *)          die "option inconnue : $1" ;;
    esac
done

case "$MODE" in
    discover) do_discover "$ROOT" ;;
    plan)     do_plan  "$ROOT" "$FILTER" ;;
    apply)    do_apply "$ROOT" "$FILTER" "$MSGFILE" "$DRY" "$NOPR" ;;
    *)        die "usage: bulk_commit.sh {discover|plan|apply} [--root PATH] [--repo NAME] [--messages FILE] [--dry-run] [--no-pr]" ;;
esac
