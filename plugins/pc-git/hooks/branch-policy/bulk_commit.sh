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

# Fenêtre d'attente CI par PR pour `--watch-ci`, en secondes. PAR PR, jamais globale :
# le coût assumé est 15 min × N repos (story § « Durcir pc-git:commit-and-pr », défaut
# écarté = watch par défaut). Overridable pour les tests.
BULK_CI_TIMEOUT="${BULK_CI_TIMEOUT:-900}"
# Délai avant l'unique re-probe « des checks existent-ils ? » — couvre la course entre
# `gh pr create` et l'enregistrement des workflows. Un seul deuxième essai, pas une boucle.
BULK_CI_GRACE="${BULK_CI_GRACE:-10}"
# Borne des appels `gh` courts du watch (probe des checks, `run list`, `run view`) —
# distincte de `BULK_CI_TIMEOUT` qui borne le `--watch` lui-même. Overridable pour les tests.
BULK_CI_PROBE_TIMEOUT="${BULK_CI_PROBE_TIMEOUT:-60}"
# Intervalle du poll de secours GitLab (`_gl_poll_fallback`) — fixe au sens de
# NFR-0023-22 (pas adaptatif), overridable pour ne pas payer 15s réelles par cas
# de test qui traverse le fallback.
BULK_CI_POLL_INTERVAL="${BULK_CI_POLL_INTERVAL:-15}"

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
    printf 'No git repo under this root — it is probably stale\n'
    printf '(moved workspace?). Check --root before concluding anything.\n'
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
    [ -n "$filter" ] || printf 'bulk_commit: note: unfiltered plan — staging every dirty repo under %s\n' "$root" >&2

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
        || { printf 'NOTHING-TO-COMMIT (%d repo(s) scanned, all clean)\n' "$scanned"; return 0; }

    # Groupes = empreintes distinctes, dans l'ordre de première apparition.
    local -a seen=()
    local i j known
    for i in "${!fps[@]}"; do
        known=0
        for j in "${seen[@]:-}"; do [ "$j" = "${fps[$i]}" ] && known=1 && break; done
        [ $known -eq 1 ] || seen+=("${fps[$i]}")
    done

    printf '# %d repo(s) to commit, %d distinct diff group(s)\n\n' \
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
        printf 'DIFF (truncated to 120 lines, identical for the whole group):\n'
        git -C "${members[0]}" diff --staged | head -120
        printf '=== END %s\n\n' "$fp"
    done

    printf 'Write ONE message per group, then:\n'
    printf '  bulk_commit.sh apply --messages <file>   # lines "<fingerprint><TAB><message>"\n'
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

# ── watch CI (`--watch-ci`) ───────────────────────────────────────────────────
# Après l'ouverture d'une PR/MR, attend le verdict CI et l'émet en UNE ligne
# machine `<verdict>\t<url>\t<log|->` — verdict ∈ pass | fail | no-ci | timeout.
# Opt-in (NFR-0023-18) : sans `--watch-ci`, aucun de ces appels n'a lieu.
#
# GitHub : pas de boucle de polling maison — `gh pr checks --watch --fail-fast`
# EST la primitive (elle boucle et sort à la première défaite).
# GitLab : `glab` n'a pas d'équivalent fiable (`glab ci status --wait` bloque
# mais son exit-code n'est pas fiable — gitlab-org/cli#7915 — et casse sur une
# branche de MR avec Merged Results Pipelines — #1017) → le verdict est lu par
# l'API du pipeline de la MR, jamais dérivé de l'exit-code de `--wait`.

# `_bounded_cmd <bin> <dir> <secs> <args…>` : lance `<bin>` (`gh` ou `glab`) dans
# `<dir>`, borné à `<secs>` (watchdog bash — `timeout(1)` absent de macOS).
# stdout/stderr séparés dans `$_BCMD_OUT` / `$_BCMD_ERR` (globals — bash ne rend
# pas deux valeurs). Exit : 0 = ok · 124 = borne atteinte · sinon celui de `<bin>`.
# Chaque appel `gh`/`glab` du watch passe par ici — sinon un appel API bloqué
# ferait hang `$(_watch_ci_github …)`/`$(_watch_ci_gitlab …)` bien au-delà du
# timeout annoncé. Portée : uniquement les appels DU WATCH — `open_pr` appelle
# `gh`/`glab` (view/create) hors de `_bounded_cmd`, préexistant, hors scope ici.
#
# Sous-shells `&` redirigés hors du pipe de `$(…)` (fichiers / `/dev/null`) sinon
# la substitution de commande reste bloquée tant qu'un `sleep` tient le fd stdout.
# `exec <bin>` : `$pid` EST le process `<bin>`, le watchdog le tue directement.
# `wdflag` retiré au retour : un `sleep` orphelin réveillé plus tard ne tire alors
# aucun `kill` (le PID pourrait être recyclé). `pkill -P` peut manquer (busybox) —
# `|| true`, au pire un `sleep` orphelin inoffensif.
_BCMD_OUT=""
_BCMD_ERR=""
_bounded_cmd() {
    local bin="$1" dir="$2" secs="$3"; shift 3
    local outf errf wdflag pid wd rc
    outf="$(mktemp "${TMPDIR:-/tmp}/bcmd_out.XXXXXX")"
    errf="$(mktemp "${TMPDIR:-/tmp}/bcmd_err.XXXXXX")"
    wdflag="$(mktemp "${TMPDIR:-/tmp}/bcmd_wd.XXXXXX")"
    ( cd "$dir" && exec "$bin" "$@" ) >"$outf" 2>"$errf" &
    pid=$!
    # `pkill -P` avant le `kill` : tant que `<bin>` vit, ses éventuels enfants (ou,
    # en test, le `sleep` du stub) sont visibles ; après, ils sont reparentés à init.
    ( sleep "$secs"
      [ -e "$wdflag" ] || exit 0
      pkill -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null ) >/dev/null 2>&1 &
    wd=$!
    if wait "$pid" 2>/dev/null; then rc=0; else rc=$?; fi
    rm -f "$wdflag"
    pkill -P "$wd" 2>/dev/null || true
    kill -TERM "$wd" 2>/dev/null || true
    wait "$wd" 2>/dev/null || true
    [ "$rc" = "143" ] && rc=124   # tué par le watchdog (SIGTERM) → borne atteinte
    _BCMD_OUT="$(cat "$outf")"; _BCMD_ERR="$(cat "$errf")"
    rm -f "$outf" "$errf"
    return "$rc"
}

# Nombre de checks sur la PR : un entier, `0` si `gh` dit « no checks », `-1` sinon.
# stderr (warnings `gh`) n'entre jamais dans le parse numérique (finding review #3).
_check_count() {
    local dir="$1" url="$2" n
    _bounded_cmd gh "$dir" "$BULK_CI_PROBE_TIMEOUT" pr checks "$url" --json state --jq 'length' || true
    printf '%s' "$_BCMD_ERR" | grep -qi 'no checks' && { printf '0'; return; }
    # `|| true` : `grep` sans match / `head` SIGPIPE sortent non-zéro — inoffensif
    # aujourd'hui (errexit désarmé dans `$(…)`), pas si un jour `inherit_errexit`.
    n="$(printf '%s' "$_BCMD_OUT" | grep -oE '[0-9]+' | head -1 || true)"
    printf '%s' "${n:--1}"
}

_watch_ci_github() {
    local repo="$1" branch="$2" url="$3"
    local count rc started elapsed logf runid

    # `no-ci` tranché AVANT tout watch (une PR sans check tournerait jusqu'au timeout).
    # `BULK_CI_GRACE` : une seule re-probe, la course post-`gh pr create` (cli/cli#7401).
    count="$(_check_count "$repo" "$url")"
    if [ "$count" = "0" ]; then
        sleep "$BULK_CI_GRACE"
        count="$(_check_count "$repo" "$url")"
    fi
    [ "$count" = "0" ] && { printf 'no-ci\t%s\t-\n' "$url"; return 0; }

    started=$SECONDS
    if _bounded_cmd gh "$repo" "$BULK_CI_TIMEOUT" pr checks "$url" --watch --fail-fast --interval 15
    then rc=0; else rc=$?; fi
    elapsed=$((SECONDS - started))

    # « no checks » arrivé tard malgré la re-probe : `--watch` sort non-zéro mais
    # stderr le dit → `no-ci`, jamais `fail` (finding review #2 ; `gh` sort 1 ou 16
    # sans check selon la version — cli/cli#9691).
    if printf '%s' "$_BCMD_ERR" | grep -qi 'no checks'; then
        printf 'no-ci\t%s\t-\n' "$url"; return 0
    fi
    if [ "$rc" = "124" ] || [ "$elapsed" -ge "$BULK_CI_TIMEOUT" ]; then
        printf 'timeout\t%s\t-\n' "$url"; return 0
    fi
    [ "$rc" -eq 0 ] && { printf 'pass\t%s\t-\n' "$url"; return 0; }

    # fail : extrait borné du log de la dernière run. `log_path` peut rester `-` si
    # aucune run n'est retrouvée — `references/ci-reaction.md` § `fail` couvre ce cas.
    logf="$(mktemp "${TMPDIR:-/tmp}/bulk_ci_fail.XXXXXX")"
    _bounded_cmd gh "$repo" "$BULK_CI_PROBE_TIMEOUT" run list --branch "$branch" --limit 1 \
        --json databaseId --jq '.[0].databaseId' || true
    runid="$(printf '%s' "$_BCMD_OUT" | grep -oE '[0-9]+' | head -1 || true)"
    if [ -n "$runid" ]; then
        _bounded_cmd gh "$repo" "$((BULK_CI_PROBE_TIMEOUT * 2))" run view "$runid" --log-failed || true
        printf '%s' "$_BCMD_OUT" | tail -80 > "$logf"
    fi
    [ -s "$logf" ] || { rm -f "$logf"; logf="-"; }
    printf 'fail\t%s\t%s\n' "$url" "$logf"
}

# ── watch CI — GitLab ─────────────────────────────────────────────────────────
# `iid` de la MR, extrait de son URL (`…/-/merge_requests/<iid>`) — insensible au
# nombre de (sous-)groupes devant, IMP-0023-28.
_mr_iid() {
    printf '%s' "$1" | grep -oE 'merge_requests/[0-9]+' | grep -oE '[0-9]+' | tail -1
}

# Nombre de pipelines DE LA MR (jamais de branche) : `0` si la liste est vide,
# vide si l'appel échoue (auth, projet introuvable, JSON illisible…) — distinct
# de `0`, jamais traité comme `no-ci` (FR-0023-49 : `no-ci` n'est tranché que
# sur une liste vide confirmée, pas sur une erreur).
# `glab api` n'a pas de `--jq` (flag inexistant sur `glab` réel, ex. 1.116.0 —
# testé en direct, `ERROR: Unknown flag: --jq` ; masqué jusqu'ici par un stub de
# test trop permissif) : le filtrage passe par le vrai binaire `jq` sur la
# sortie JSON brute. `jq` absent ou JSON illisible → `_BCMD_OUT` vide en sortie,
# donc `count`/`status` vides, donc `timeout` en bout de chaîne — même
# dégradation que les autres causes « statut illisible » déjà documentées.
_gl_pipeline_count() {
    local repo="$1" iid="$2"
    _bounded_cmd glab "$repo" "$BULK_CI_PROBE_TIMEOUT" \
        api "projects/:id/merge_requests/${iid}/pipelines" || true
    # `grep -oE` en aval de `jq` : ne fait pas confiance aveuglément à un champ
    # numérique venu de l'API, même déjà filtré.
    printf '%s' "$_BCMD_OUT" | jq -r 'length' 2>/dev/null | grep -oE '[0-9]+' | head -1
}

# `status` du dernier pipeline DE LA MR (`.[0]`, jamais `glab ci status` — #1017).
# Chaîne vide si l'appel échoue ou si le pipeline n'a pas (encore) de statut
# (`// empty` — `jq -r` rendrait sinon la chaîne littérale `null`).
_gl_status() {
    local repo="$1" iid="$2"
    _bounded_cmd glab "$repo" "$BULK_CI_PROBE_TIMEOUT" \
        api "projects/:id/merge_requests/${iid}/pipelines" || true
    printf '%s' "$_BCMD_OUT" | jq -r '.[0].status // empty' 2>/dev/null | tr -d '[:space:]'
}

# Boucle de secours si `--wait` est indisponible/cassé, ou hang sur un statut
# non-terminal : poll intervalle fixe 15s, plafonné à `BULK_CI_TIMEOUT` depuis
# `$3`, PUIS une seule re-probe de grâce (NFR-0023-22 — pas une primitive
# réutilisable, ce chemin est le seul appelant).
_gl_poll_fallback() {
    local repo="$1" iid="$2" started="$3" status elapsed
    while :; do
        status="$(_gl_status "$repo" "$iid")"
        case "$status" in
            success|failed|canceled|skipped|manual) printf '%s' "$status"; return 0 ;;
        esac
        elapsed=$((SECONDS - started))
        [ "$elapsed" -lt "$BULK_CI_TIMEOUT" ] || break
        sleep "$BULK_CI_POLL_INTERVAL"
    done
    sleep "$BULK_CI_GRACE"
    _gl_status "$repo" "$iid"
}

# `skipped` au niveau pipeline recouvre deux cas indiscernables sans regarder les
# jobs : aucune CI réelle (vrai `no-ci`) et un gate manuel jamais déclenché — un
# choix de process, pas un échec CI (FR-0023-48). GitLab n'expose jamais
# `status: "manual"` au niveau pipeline pour ce cas (confirmé sur witness réel,
# repo `pinky-ci-watch-witness` MR `!3` : pipeline `skipped`, job `manual`) —
# seul le job le porte, d'où ce détour par `…/pipelines/<id>/jobs`.
_gl_has_manual_job() {
    local repo="$1" iid="$2" pid
    _bounded_cmd glab "$repo" "$BULK_CI_PROBE_TIMEOUT" \
        api "projects/:id/merge_requests/${iid}/pipelines" || true
    pid="$(printf '%s' "$_BCMD_OUT" | jq -r '.[0].id // empty' 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    [ -n "$pid" ] || return 1
    _bounded_cmd glab "$repo" "$BULK_CI_PROBE_TIMEOUT" \
        api "projects/:id/pipelines/${pid}/jobs" || true
    printf '%s' "$_BCMD_OUT" | jq -e '[.[] | select(.status=="manual")] | length > 0' \
        >/dev/null 2>&1
}

# Extrait de trace (≤80 lignes) du premier job `failed` du pipeline de la MR.
# `-` si le pipeline, le job ou la trace ne sont pas retrouvables (FR-0023-50).
_gl_fail_log() {
    local repo="$1" iid="$2" pid jid logf
    _bounded_cmd glab "$repo" "$BULK_CI_PROBE_TIMEOUT" \
        api "projects/:id/merge_requests/${iid}/pipelines" || true
    pid="$(printf '%s' "$_BCMD_OUT" | jq -r '.[0].id // empty' 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    [ -n "$pid" ] || { printf -- '-'; return; }
    _bounded_cmd glab "$repo" "$BULK_CI_PROBE_TIMEOUT" \
        api "projects/:id/pipelines/${pid}/jobs" || true
    jid="$(printf '%s' "$_BCMD_OUT" | jq -r '[.[]|select(.status=="failed")][0].id // empty' 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    [ -n "$jid" ] || { printf -- '-'; return; }
    logf="$(mktemp "${TMPDIR:-/tmp}/bulk_ci_fail_gl.XXXXXX")"
    _bounded_cmd glab "$repo" "$((BULK_CI_PROBE_TIMEOUT * 2))" \
        api "projects/:id/jobs/${jid}/trace" || true
    printf '%s' "$_BCMD_OUT" | tail -80 > "$logf"
    [ -s "$logf" ] || { rm -f "$logf"; logf="-"; }
    printf '%s' "$logf"
}

_watch_ci_gitlab() {
    local repo="$1" url="$2"
    local iid count started status logf

    iid="$(_mr_iid "$url")"
    # `iid` illisible (URL de forme inattendue) : ce n'est ni « pas de pipeline »
    # ni un statut terminal — `timeout`, jamais `no-ci` (review bloquante #2 :
    # `no-ci` est traité comme `pass` côté parent, un cas non tranché ne doit
    # jamais y atterrir).
    [ -n "$iid" ] || { printf 'timeout\t%s\t-\n' "$url"; return 0; }

    # `no-ci` tranché AVANT tout wait/poll : liste de pipelines vide confirmée,
    # jamais sur un appel en échec (FR-0023-49). Re-probe de grâce avant de
    # conclure : même course post-`mr create` que côté GitHub (cli/cli#7401),
    # et déterminante ici sous « Merged Results Pipelines » (#1017) où le
    # pipeline de MR n'existe qu'après l'événement MR — review bloquante #1.
    count="$(_gl_pipeline_count "$repo" "$iid")"
    if [ "$count" = "0" ]; then
        sleep "$BULK_CI_GRACE"
        count="$(_gl_pipeline_count "$repo" "$iid")"
    fi
    [ "$count" = "0" ] && { printf 'no-ci\t%s\t-\n' "$url"; return 0; }

    started=$SECONDS
    _bounded_cmd glab "$repo" "$BULK_CI_TIMEOUT" ci status --wait >/dev/null 2>&1 || true
    # L'exit-code de `--wait` n'est jamais fiable (gitlab-org/cli#7915) : on relit
    # toujours le `status` par l'API plutôt que de s'y fier.
    if printf '%s' "$_BCMD_ERR" | grep -qi 'unknown flag\|unknown command'; then
        status="$(_gl_poll_fallback "$repo" "$iid" "$started")"
    else
        status="$(_gl_status "$repo" "$iid")"
        case "$status" in
            success|failed|canceled|skipped|manual) : ;;
            # `--wait` borné par `_bounded_cmd` peut revenir avant un statut
            # terminal (hang sur `manual`/`blocked`, erreur `glab` ambiguë) :
            # une dernière poll de secours avant de conclure (IMP-0023-27).
            *) status="$(_gl_poll_fallback "$repo" "$iid" "$started")" ;;
        esac
    fi

    case "$status" in
        success) printf 'pass\t%s\t-\n' "$url" ;;
        manual)  printf 'pass\t%s\t-\n' "$url" ;;
        failed|canceled)
            logf="$(_gl_fail_log "$repo" "$iid")"
            printf 'fail\t%s\t%s\n' "$url" "$logf" ;;
        skipped)
            if _gl_has_manual_job "$repo" "$iid"; then
                printf 'pass\t%s\t-\n' "$url"
            else
                printf 'no-ci\t%s\t-\n' "$url"
            fi ;;
        # statut vide/non-terminal persistant (auth KO, projet introuvable…) :
        # jamais `pass` par défaut (IMP-0023-27).
        *) printf 'timeout\t%s\t-\n' "$url" ;;
    esac
}

# ── apply ─────────────────────────────────────────────────────────────────────
do_apply() {
    local root="$1" filter="$2" msgfile="$3" dry="$4" nopr="$5" watchci="$6"
    [ -f "$msgfile" ] || die "message file not found: $msgfile"

    local ok=0 failed=0 skipped=0 partial=0
    local repo name fp msg strategy base head branch slug prv state url
    local -a results=() ci_results=()

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
            printf '  ⏭️  %-28s no message for fingerprint %s\n' "$name" "$fp"
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
            printf '  ❌ %-28s branch %s failed (changes staged, nothing lost)\n' \
                "$name" "$branch"
            results+=("$(printf '%s\tfailed\t-' "$name")")
            failed=$((failed + 1)); continue
        fi
        if ! git -C "$repo" commit -m "$msg" -m "$trailer" >/dev/null 2>&1; then
            printf '  ❌ %-28s commit refused, likely a hook (changes staged, nothing lost)\n' "$name"
            results+=("$(printf '%s\tfailed\t-' "$name")")
            failed=$((failed + 1)); continue
        fi
        if ! git -C "$repo" push -u origin "$branch" >/dev/null 2>&1; then
            printf '  ⚠️  %-28s committed on %s, push failed (local commit kept)\n' \
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
                pr)         state="pushed-pr";       printf '      PR opened: %s\n' "$url" ;;
                pr-open)    state="pushed-pr";       printf '      PR already open: %s\n' "$url" ;;
                pr-na)      state="pushed-nopr";     printf '      direct commit on %s, no PR\n' "$base" ;;
                *)          state="pushed-prfailed"; url="-"
                            printf '      ⚠️  PR not opened — open it manually\n' ;;
            esac
        fi
        results+=("$(printf '%s\t%s\t%s' "$name" "$state" "${url:--}")")
        # `pushed-prfailed` : commité + poussé, mais une PR reste à ouvrir à la main —
        # compté comme partiel, jamais dans le ✅.
        [ "$state" = "pushed-prfailed" ] && partial=$((partial + 1)) || ok=$((ok + 1))

        # `--watch-ci` : seulement pour une PR/MR réellement ouverte.
        if [ "$watchci" = "yes" ] && [ "$state" = "pushed-pr" ] && [ "$url" != "-" ]; then
            printf '      ⏳ watching CI (watch ≤%ss/PR + bounded probes)…\n' "$BULK_CI_TIMEOUT"
            case "$(git -C "$repo" remote get-url origin 2>/dev/null || true)" in
                *gitlab*)
                    ci_results+=("$(printf '%s\t%s' "$name" \
                        "$(_watch_ci_gitlab "$repo" "$url")")") ;;
                *)
                    ci_results+=("$(printf '%s\t%s' "$name" \
                        "$(_watch_ci_github "$repo" "$branch" "$url")")") ;;
            esac
        fi
    done < <(discover_repos "$root")

    # ⚠️ compté à part : un commit fait mais non poussé (ou une PR à ouvrir à la main)
    # n'est ni un succès ni une perte.
    printf '\n✅ %d   ⚠️  %d   ⏭️  %d   ❌ %d\n' "$ok" "$partial" "$skipped" "$failed"

    # Bloc machine : l'agent (Step 4 du skill) le reproduit tel quel, sans en dériver une
    # phrase absente. `state` ∈ pushed-pr | pushed-nopr | pushed-prfailed | commit-nopush
    # | skipped | failed. Correctif de l'incident 2026-09-02 (résumé forké inventé).
    # Sauté en `--dry-run` : rien n'a été fait, un bloc vide serait un piège pour l'agent.
    if [ "$dry" != "yes" ]; then
        printf '\n=== RESULTS (repo\tstate\tpr_url) — reproduce verbatim\n'
        local line
        for line in "${results[@]:-}"; do [ -n "$line" ] && printf '%s\n' "$line"; done
        printf '=== END RESULTS\n'

        # Bloc CI : présent UNIQUEMENT avec `--watch-ci` (NFR-0023-18 — sans le flag,
        # rien ici, et aucun `gh pr checks` n'a été appelé). L'agent le reproduit tel
        # quel ; `verdict` ∈ pass | fail | no-ci | timeout ; `log_path` pointe l'extrait
        # de log (`fail` seulement), à lire côté parent — cf. `references/ci-reaction.md`.
        if [ "$watchci" = "yes" ]; then
            printf '\n=== CI (repo\tverdict\turl\tlog_path) — reproduce verbatim\n'
            local cline
            for cline in "${ci_results[@]:-}"; do
                [ -n "$cline" ] && printf '%s\n' "$cline"
            done
            printf '=== END CI\n'
        fi
    fi

    [ "$failed" -eq 0 ]
}

# ── CLI ───────────────────────────────────────────────────────────────────────
MODE="${1:-}"; shift || true
ROOT="$PWD"; FILTER=""; MSGFILE=""; DRY="no"; NOPR="no"; WATCHCI="no"
while [ $# -gt 0 ]; do
    case "$1" in
        --root)     ROOT="$2"; shift 2 ;;
        --repo)     FILTER="$2"; shift 2 ;;
        --messages) MSGFILE="$2"; shift 2 ;;
        --dry-run)  DRY="yes"; shift ;;
        --no-pr)    NOPR="yes"; shift ;;
        --watch-ci) WATCHCI="yes"; shift ;;
        *)          die "unknown option: $1" ;;
    esac
done

case "$MODE" in
    discover) do_discover "$ROOT" ;;
    plan)     do_plan  "$ROOT" "$FILTER" ;;
    apply)    do_apply "$ROOT" "$FILTER" "$MSGFILE" "$DRY" "$NOPR" "$WATCHCI" ;;
    *)        die "usage: bulk_commit.sh {discover|plan|apply} [--root PATH] [--repo NAME] [--messages FILE] [--dry-run] [--no-pr] [--watch-ci]" ;;
esac
