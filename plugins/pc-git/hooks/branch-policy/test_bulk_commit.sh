#!/usr/bin/env bash
# Test du groupement de bulk_commit.sh — le comportement qui porte tout le gain.
#
# Ce qu'il verrouille : des repos recevant une distribution IDENTIQUE doivent tomber dans
# UN groupe, y compris quand l'un d'eux traînait un `.DS_Store`. Sans l'exclusion du
# `.gitignore` dans l'empreinte, ce repo-là sort du groupe et le lot se fragmente —
# c'est le bug trouvé au premier test réel, et c'est lui que ce fichier empêche de revenir.

set -uo pipefail

# Un hook git (prek/pre-commit) exporte `GIT_INDEX_FILE`, parfois `GIT_DIR` : ils redirigent la
# découverte du repo quel que soit `-C`, donc les `init`/`add`/`commit` des repos jetables ci-dessous
# écriraient dans l'index et le `.git/config` du vrai repo. Purgés AVANT l'identité de test.
for _v in $(compgen -e | grep '^GIT_' || true); do unset "$_v"; done

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bulk_commit.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

# Le watch CI attend `BULK_CI_STARTUP_DELAY` (défaut 60s en prod) avant sa 1ʳᵉ probe —
# désarmé pour toute la suite, sinon chaque cas watch paierait 60s réelles.
export BULK_CI_STARTUP_DELAY=0

# Identité git pour les commits que `bulk_commit.sh` fait lui-même — un runner CI vierge
# n'a aucun `user.name`/`user.email` global, `git commit` y échoue et tout le reste
# (push, PR, watch) est sauté en cascade. Les repos de test sont jetables.
export GIT_AUTHOR_NAME="bulk-commit-test" GIT_AUTHOR_EMAIL="test@test.invalid"
export GIT_COMMITTER_NAME="bulk-commit-test" GIT_COMMITTER_EMAIL="test@test.invalid"

fail=0
check() {
    if [ "$2" = "$3" ]; then
        printf '  ✅ %s\n' "$1"
    else
        printf '  ❌ %s — attendu "%s", obtenu "%s"\n' "$1" "$3" "$2"; fail=1
    fi
}

mkrepo() {
    mkdir -p "$T/$1" && git -C "$T/$1" init -q -b develop
    git -C "$T/$1" commit -q --allow-empty -m init
}

# Trois repos, une seule et même distribution (un `.md` → chemin non gardé).
for r in alpha bravo charlie; do mkrepo "$r"; printf 'shared\n' > "$T/$r/README.md"; done
printf 'junk' > "$T/alpha/.DS_Store"          # bruit sur UN SEUL des trois
mkrepo delta; printf 'def f():\n    return 1\n' > "$T/delta/app.py"   # code → chemin gardé
mkrepo echo_; printf 'junk' > "$T/echo_/.DS_Store"                    # bruit et rien d'autre
mkrepo golf-clean                                                     # propre : jamais listé

# `discover` — READ-ONLY : liste les repos sales, ne stage rien (le skill résout le scope
# avec, AVANT `plan` qui stage). Testé avant tout `plan`.
disc="$("$SCRIPT" discover --root "$T" 2>&1)"; disc_rc=$?
check "discover lists the 5 dirty repos"   "$(printf '%s\n' "$disc" | sort | tr '\n' ' ')" \
    "alpha bravo charlie delta echo_ "
check "discover exits 0 even when the last-sorted repo (golf-clean) is clean" "$disc_rc" 0
check "discover stages nothing (delta)"     "$(git -C "$T/delta" diff --cached --name-only | wc -l | tr -d ' ')" 0
check "discover writes no .gitignore"  "$([ -e "$T/alpha/.gitignore" ] && echo yes || echo no)" no
# Racine sans dépôt : `discover` doit dire NO-REPOS-FOUND + exit non nul, jamais une
# sortie vide qui se confondrait avec "des repos, tous propres".
demptyd="$(mktemp -d)"
outd="$("$SCRIPT" discover --root "$demptyd" 2>&1)"; rcd=$?
rmdir "$demptyd"
check "discover empty root -> NO-REPOS-FOUND"  "$(printf '%s' "$outd" | grep -c 'NO-REPOS-FOUND')" 1
check "discover empty root -> non-zero exit"    "$([ "$rcd" -ne 0 ] && echo yes || echo no)"        yes

plan="$("$SCRIPT" plan --root "$T" 2>&1)"

check "5 repos detected"          "$(printf '%s' "$plan" | grep -c '^  [❌⚠️⏭️]')"                    7
check "3 distinct groups"       "$(printf '%s' "$plan" | grep -c '^=== GROUP')"              3
# Taille de chaque groupe (nombre de lignes de repo entre GROUP et END) : le groupe de la
# distribution partagée doit en compter 3. Compter par `grep -A<n>` ne marche pas — la
# fenêtre fixe tronque le groupe dès qu'il dépasse n lignes.
sizes="$(printf '%s\n' "$plan" | awk '/^=== GROUP/{n=0} /^  \//{n++} /^=== END/{print n}' | sort -rn | head -1)"
check "noise does not fragment the group" "$sizes" 3
check "code -> feature branch"     "$(printf '%s' "$plan" | grep 'delta' | grep -c 'feature-branch=yes')" 1
check "docs -> direct commit"      "$(printf '%s' "$plan" | grep 'bravo' | grep -c 'feature-branch=no')"  1
check "noise group isolated"     "$(printf '%s' "$plan" | grep -c 'GROUP noise-gitignore')"   1
check ".DS_Store gitignored"       "$(grep -c '^\.DS_Store$' "$T/alpha/.gitignore")"            1

# apply --dry-run ne doit rien committer, et doit couvrir les 5 repos.
printf '%s\t📝 docs: add README\n' "$(printf '%s' "$plan" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)" > "$T/m.tsv"
dry="$("$SCRIPT" apply --root "$T" --messages "$T/m.tsv" --dry-run 2>&1)"
check "dry-run commits nothing"  "$(git -C "$T/bravo" log --oneline | wc -l | tr -d ' ')"     1
check "group with no message -> skipped"  "$(printf '%s' "$dry" | grep -c 'no message')"            1
check "unfiltered plan -> stderr note (not a die)" \
    "$("$SCRIPT" plan --root "$T" 2>&1 >/dev/null | grep -c 'note: unfiltered plan')"           1

# Une racine sans aucun dépôt n'est pas un workspace propre : c'est une racine fausse.
# Les confondre annonce "rien à faire" après avoir regardé au mauvais endroit — vu en vrai
# juste après un déplacement de workspace, avec 11 repos sales laissés au sol.
empty="$(mktemp -d)"
out="$("$SCRIPT" plan --root "$empty" 2>&1)"; rc=$?
rmdir "$empty"
check "empty root -> NO-REPOS-FOUND" "$(printf '%s' "$out" | grep -c 'NO-REPOS-FOUND')" 1
check "empty root -> non-zero exit"   "$([ "$rc" -ne 0 ] && echo yes || echo no)"          yes
check "never NOTHING-TO-COMMIT"     "$(printf '%s' "$out" | grep -c 'NOTHING-TO-COMMIT')" 0

# open_pr() ne doit jamais confondre "aucune bascule faite ici" avec "on est sur la branche
# d'intégration" — un HEAD déjà sur une feature branch avant l'appel a droit à sa PR comme
# n'importe quelle branche créée par le script lui-même.
bare="$T/origin.git"
git init -q --bare -b main "$bare"
mkdir -p "$T/foxtrot" && git -C "$T/foxtrot" init -q -b main
git -C "$T/foxtrot" remote add origin "$bare"
git -C "$T/foxtrot" commit -q --allow-empty -m init
git -C "$T/foxtrot" push -q -u origin main
git -C "$T/foxtrot" remote set-head origin main
git -C "$T/foxtrot" checkout -q -b feature/preexisting
printf 'def g():\n    return 2\n' > "$T/foxtrot/lib.py"

ghbin="$T/ghbin"; mkdir -p "$ghbin"
cat > "$ghbin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$GH_LOG"
case "$1 $2" in
  "pr view")   exit 1 ;;
  "pr create") echo "https://github.com/x/foxtrot/pull/1" ;;
esac
exit 0
EOF
chmod +x "$ghbin/gh"
export GH_LOG="$T/gh.log"; : > "$GH_LOG"

planfox="$("$SCRIPT" plan --root "$T" --repo foxtrot 2>&1)"
fpfox="$(printf '%s' "$planfox" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t✨ feat: add g\n' "$fpfox" > "$T/mfox.tsv"
# Un fork commit-and-pr hérite l'env du parent (vérifié 2026-09-02) : le trailer doit
# donc être composé à partir de cette variable, jamais du system-reminder (absent d'un fork).
export CLAUDE_CODE_BRIDGE_SESSION_ID="session_TESTBRIDGE"
outfox="$(PATH="$ghbin:$PATH" "$SCRIPT" apply --root "$T" --repo foxtrot --messages "$T/mfox.tsv" --no-watch-ci 2>&1)"

check "PR attempted from a pre-existing feature branch" \
    "$(grep -c '^gh pr create' "$GH_LOG")" 1
check "stays on its branch, no new one created" \
    "$(git -C "$T/foxtrot" branch --show-current)" "feature/preexisting"

# ── Contrat de sortie : bloc RESULTS + trailer (carte durcir-commit-and-pr-contrat-de-sortie) ──
tab="$(printf '\t')"   # BSD `grep -E` n'étend pas `\t` — passer un vrai tab
# Oracle : le contenu entre `=== RESULTS` et `=== END RESULTS` est EXACTEMENT cette ligne.
results_block="$(printf '%s' "$outfox" | awk '/^=== RESULTS/{f=1;next} /^=== END RESULTS/{f=0} f')"
check "RESULTS foxtrot = exact pushed-pr line + URL" \
    "$results_block" "foxtrot${tab}pushed-pr${tab}https://github.com/x/foxtrot/pull/1"
check "=== END RESULTS marker emitted" \
    "$(printf '%s' "$outfox" | grep -c '^=== END RESULTS$')" 1
check "Co-Authored-By trailer in the commit" \
    "$(git -C "$T/foxtrot" log -1 --format=%B | grep -c '^Co-Authored-By: Claude <noreply@anthropic.com>$')" 1
check "Claude-Session trailer derived from the bridge id" \
    "$(git -C "$T/foxtrot" log -1 --format=%B | grep -c '^Claude-Session: https://claude.ai/code/session_TESTBRIDGE$')" 1

# ── Trailer : le modèle de la session PRINCIPALE, lu dans son transcript via --session-id ──
# Le fork commit-and-pr tourne `haiku` : nommer son modèle serait faux. Le transcript de la
# session (`<config>/projects/<projet>/<session-id>.jsonl`) enregistre le vrai, quel que soit
# le fournisseur ; sans id exploitable on retombe sur la ligne neutre (déjà vérifiée ci-dessus).
sid="11111111-2222-3333-4444-555555555555"
mkdir -p "$T/cfg/projects/-tmp-golf"
printf '%s\n' \
    '{"isSidechain":false,"attachment":{"type":"model","identity":{"modelId":"glm-5.3[1m]","marketingName":null,"knowledgeCutoff":null}},"type":"attachment","sessionId":"'"$sid"'"}' \
    > "$T/cfg/projects/-tmp-golf/$sid.jsonl"
bareg="$T/origin-golf.git"
git init -q --bare -b main "$bareg"
mkdir -p "$T/golf" && git -C "$T/golf" init -q -b main
git -C "$T/golf" remote add origin "$bareg"
git -C "$T/golf" commit -q --allow-empty -m init
git -C "$T/golf" push -q -u origin main
git -C "$T/golf" remote set-head origin main
git -C "$T/golf" checkout -q -b feature/golf
printf 'def h():\n    return 3\n' > "$T/golf/lib.py"
plangolf="$("$SCRIPT" plan --root "$T" --repo golf 2>&1)"
fpgolf="$(printf '%s' "$plangolf" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t✨ feat: add h\n' "$fpgolf" > "$T/mgolf.tsv"
PATH="$ghbin:$PATH" CLAUDE_CONFIG_DIR="$T/cfg" \
    "$SCRIPT" apply --root "$T" --repo golf --messages "$T/mgolf.tsv" --session-id "$sid" --no-watch-ci >/dev/null 2>&1
check "non-Anthropic session model named in the trailer, [1m] stripped" \
    "$(git -C "$T/golf" log -1 --format=%B | grep -c '^Co-Authored-By: Claude Code (glm-5.3) <noreply@anthropic.com>$')" 1
check "Claude-Session line kept next to the model line" \
    "$(git -C "$T/golf" log -1 --format=%B | grep -c '^Claude-Session: https://claude.ai/code/session_TESTBRIDGE$')" 1
printf 'def k():\n    return 4\n' > "$T/golf/lib2.py"
plangolf2="$("$SCRIPT" plan --root "$T" --repo golf 2>&1)"
fpgolf2="$(printf '%s' "$plangolf2" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t✨ feat: add k\n' "$fpgolf2" > "$T/mgolf2.tsv"
PATH="$ghbin:$PATH" CLAUDE_CONFIG_DIR="$T/cfg" \
    "$SCRIPT" apply --root "$T" --repo golf --messages "$T/mgolf2.tsv" --session-id '${CLAUDE_SESSION_ID}' --no-watch-ci >/dev/null 2>&1
check "unsubstituted session id degrades to the neutral trailer" \
    "$(git -C "$T/golf" log -1 --format=%B | grep -c '^Co-Authored-By: Claude <noreply@anthropic.com>$')" 1

# Le run foxtrot ci-dessus porte `--no-watch-ci` (le watch est ON par
# défaut désormais) — donc aucun bloc CI, et `gh pr checks` jamais appelé.
check "--no-watch-ci -> no === CI block" \
    "$(printf '%s' "$outfox" | grep -c '^=== CI ')" 0
check "--no-watch-ci -> gh pr checks never called" \
    "$(grep -c 'pr checks' "$GH_LOG")" 0

# ── pr-na : docs-only sur la branche d'intégration, remote résolu → commit direct, zéro PR ──
gbare="$T/golf-origin.git"
git init -q --bare -b main "$gbare"
mkdir -p "$T/golf" && git -C "$T/golf" init -q -b main
git -C "$T/golf" remote add origin "$gbare"
git -C "$T/golf" commit -q --allow-empty -m init
git -C "$T/golf" push -q -u origin main
git -C "$T/golf" remote set-head origin main          # résout pour de vrai (main fetché)
printf 'hello\n' > "$T/golf/README.md"                # chemin non gardé → HEAD == base == main
: > "$GH_LOG"
plang="$("$SCRIPT" plan --root "$T" --repo golf 2>&1)"
fpg="$(printf '%s' "$plang" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t📝 docs: readme\n' "$fpg" > "$T/mg.tsv"
outg="$(PATH="$ghbin:$PATH" "$SCRIPT" apply --root "$T" --repo golf --messages "$T/mg.tsv" --no-watch-ci 2>&1)"
golf_block="$(printf '%s' "$outg" | awk '/^=== RESULTS/{f=1;next} /^=== END RESULTS/{f=0} f')"
check "RESULTS golf = direct commit, exact pushed-nopr state" \
    "$golf_block" "golf${tab}pushed-nopr${tab}-"
check "golf: no PR attempted on a direct commit" \
    "$(grep -c '^gh pr create' "$GH_LOG")" 0

# ── --watch-ci : les verdicts + les régressions de review (carte session-babysitte) ──
# Stub `gh` piloté par env. stdout et stderr SÉPARÉS : `GH_CI_JSON_STDERR` /
# `GH_CI_WATCH_STDERR` simulent un warning ou un « no checks » sur stderr sans polluer
# le parse numérique (findings review #2/#3). `GH_CI_COUNT` non défini = stdout vide.
cibin="$T/cibin"; mkdir -p "$cibin"
cat > "$cibin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$GH_LOG"
case "$*" in
  "pr view"*)                    exit 1 ;;
  "pr create"*)                  echo "https://github.com/x/${GH_REPO}/pull/1"; exit 0 ;;
  *"pr checks"*"--json state"*)
      [ -n "${GH_CI_JSON_SLEEP:-}" ] && sleep "$GH_CI_JSON_SLEEP"
      [ -n "${GH_CI_JSON_STDERR:-}" ] && printf '%s\n' "$GH_CI_JSON_STDERR" >&2
      [ -n "${GH_CI_COUNT+x}" ] && printf '%s\n' "$GH_CI_COUNT"
      exit "${GH_CI_JSON_RC:-0}" ;;
  *"pr checks"*"--watch"*)
      [ -n "${GH_CI_WATCH_SLEEP:-}" ] && sleep "$GH_CI_WATCH_SLEEP"
      [ -n "${GH_CI_WATCH_STDERR:-}" ] && printf '%s\n' "$GH_CI_WATCH_STDERR" >&2
      exit "${GH_CI_WATCH_RC:-0}" ;;
  "run list"*)                   echo "424242"; exit 0 ;;
  "run view"*)                   printf 'ruff.....F\nmod.py:1:1: F401 unused import\n'; exit 0 ;;
esac
exit 0
EOF
chmod +x "$cibin/gh"

mk_watch_repo() {   # NAME → repo poussé sur `main`, checkout d'une feature branch, un .py stagé
    local n="$1" b="$T/$1-origin.git"
    git init -q --bare -b main "$b"
    mkdir -p "$T/$n" && git -C "$T/$n" init -q -b main
    git -C "$T/$n" remote add origin "$b"
    git -C "$T/$n" commit -q --allow-empty -m init
    git -C "$T/$n" push -q -u origin main
    git -C "$T/$n" remote set-head origin main
    git -C "$T/$n" checkout -q -b "feature/$n"
    printf 'def x():\n    return 1\n' > "$T/$n/mod_$n.py"
}

run_watch() {   # NAME  KEY=VAL...  → stdout de `apply --watch-ci` sur ce repo
    local n="$1"; shift
    local plan fp
    plan="$("$SCRIPT" plan --root "$T" --repo "$n" 2>&1)"
    fp="$(printf '%s' "$plan" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
    printf '%s\t✨ feat: %s\n' "$fp" "$n" > "$T/m_$n.tsv"
    env "$@" GH_REPO="$n" PATH="$cibin:$PATH" \
        "$SCRIPT" apply --root "$T" --repo "$n" --messages "$T/m_$n.tsv" --watch-ci 2>&1
}

ci_block() { printf '%s' "$1" | awk '/^=== CI /{f=1;next} /^=== END CI/{f=0} f'; }

# pass : des checks, --watch exit 0. (`BULK_CI_TIMEOUT` bas : le stub rend la main
# tout de suite, inutile d'armer un watchdog de 15 min pendant les tests.)
mk_watch_repo hotel
outh="$(run_watch hotel GH_CI_COUNT=2 GH_CI_WATCH_RC=0 BULK_CI_TIMEOUT=3)"
check "watch-ci: pass verdict, exact line" \
    "$(ci_block "$outh")" "hotel${tab}pass${tab}https://github.com/x/hotel/pull/1${tab}-"
check "watch-ci: === END CI marker emitted" \
    "$(printf '%s' "$outh" | grep -c '^=== END CI$')" 1

# fail : --watch exit non nul → verdict fail + chemin d'extrait de log.
mk_watch_repo india
outi="$(run_watch india GH_CI_COUNT=2 GH_CI_WATCH_RC=1 BULK_CI_TIMEOUT=3)"
ib="$(ci_block "$outi")"
check "watch-ci: fail verdict + url (cols 1-3)" \
    "$(printf '%s' "$ib" | cut -f1-3)" "india${tab}fail${tab}https://github.com/x/india/pull/1"
check "watch-ci: fail carries a log-extract path" \
    "$(printf '%s' "$ib" | cut -f4 | grep -c '^/')" 1
check "watch-ci: the log extract is non-empty" \
    "$([ -s "$(printf '%s' "$ib" | cut -f4)" ] && echo yes || echo no)" yes

# no-ci : zéro check → jamais de --watch, jamais `pass`.
: > "$GH_LOG"
mk_watch_repo juliett
outj="$(run_watch juliett GH_CI_COUNT=0 BULK_CI_GRACE=0)"
check "watch-ci: no-ci verdict (never pass)" \
    "$(ci_block "$outj")" "juliett${tab}no-ci${tab}https://github.com/x/juliett/pull/1${tab}-"
check "watch-ci: no-ci never enters --watch" \
    "$(grep -c 'pr checks .*--watch' "$GH_LOG")" 0

# timeout : le --watch dépasse la fenêtre (rabaissée à 1s) → verdict timeout, jamais un faux pass.
mk_watch_repo kilo
outk="$(run_watch kilo GH_CI_COUNT=2 GH_CI_WATCH_RC=0 GH_CI_WATCH_SLEEP=4 BULK_CI_TIMEOUT=1 BULK_CI_GRACE=0)"
check "watch-ci: timeout verdict (cols 1-2)" \
    "$(ci_block "$outk" | cut -f1-2)" "kilo${tab}timeout"

# ── Régressions review (2026-09-03) ──────────────────────────────────────────

# #2 — « no checks » arrivé tard : le probe JSON échoue SANS dire « no checks »
# (count -> -1, la voie rapide no-ci est sautée), puis --watch sort non-zéro AVEC
# « no checks » sur stderr → verdict `no-ci`, jamais `fail\t<url>\t-`.
mk_watch_repo lima
outl="$(run_watch lima GH_CI_JSON_RC=1 GH_CI_JSON_STDERR='could not resolve to a Repository' \
    GH_CI_WATCH_RC=1 GH_CI_WATCH_STDERR="no checks reported on the 'feature/lima' branch" \
    BULK_CI_TIMEOUT=3 BULK_CI_GRACE=0)"
check "watch-ci #2: late 'no checks' on --watch stderr -> no-ci, not fail" \
    "$(ci_block "$outl")" "lima${tab}no-ci${tab}https://github.com/x/lima/pull/1${tab}-"

# #3 — stdout dit « 0 » mais stderr porte un warning `gh` : le warning ne doit PAS
# entrer dans le parse (sinon count -> -1 -> voie rapide sautée -> --watch inutile).
: > "$GH_LOG"
mk_watch_repo mike
outm="$(run_watch mike GH_CI_COUNT=0 GH_CI_JSON_STDERR='warning: token scope' BULK_CI_GRACE=0)"
check "watch-ci #3: count 0 + stderr warning -> no-ci (stderr not merged into parse)" \
    "$(ci_block "$outm")" "mike${tab}no-ci${tab}https://github.com/x/mike/pull/1${tab}-"
check "watch-ci #3: never entered --watch" \
    "$(grep -c 'pr checks .*--watch' "$GH_LOG")" 0

# #1 — un appel `gh` court qui bloque (ici le probe JSON) est borné : probe borné à
# 1s vs stub qui dort 9s. Sans la borne le run durerait ≥9s ; avec, ~1s. On mesure
# le mur, pas juste le verdict. Probe tué → count indéterminé → `--watch` (RC 0) → pass.
mk_watch_repo november
t0=$SECONDS
outn="$(run_watch november GH_CI_JSON_SLEEP=9 GH_CI_WATCH_RC=0 \
    BULK_CI_PROBE_TIMEOUT=1 BULK_CI_TIMEOUT=3 BULK_CI_GRACE=0)"
dt=$((SECONDS - t0))
check "watch-ci #1: stalled probe bounded, verdict still reached" \
    "$(ci_block "$outn" | cut -f1-2)" "november${tab}pass"
check "watch-ci #1: bound fired — wall time well under the 9s stall" \
    "$([ "$dt" -lt 7 ] && echo yes || echo no)" yes

# ── Inspection : chaque appel `glab` du bloc watch GitLab passe par
# `_bounded_cmd` — `open_pr` (`mr view`/`mr create`) est hors scope, précédent déjà
# accepté côté `gh` (carte watch-ci-support-gitlab).
gl_watch_src="$(awk '/^# ── watch CI — GitLab/,/^# ── apply/' "$SCRIPT")"
bad_glab="$(printf '%s\n' "$gl_watch_src" | grep -v '^[[:space:]]*#' | grep -v '_bounded_cmd glab' | grep -c 'glab' || true)"
check "every glab call in the GitLab watch path goes through _bounded_cmd" "$bad_glab" 0

# Régression : `glab api --jq` n'existe pas sur `glab` réel (testé en direct sur
# 1.116.0, `ERROR: Unknown flag: --jq`) — un stub trop permissif avait laissé ce
# flag inerte passer 67/67 verts pendant tout le développement de cette carte.
no_jq_flag="$(printf '%s\n' "$gl_watch_src" | grep -v '^[[:space:]]*#' | grep -c -- '--jq' || true)"
check "no --jq flag left in the GitLab watch path (glab api has none)" "$no_jq_flag" 0

# ── --watch-ci GitLab : mapping des verdicts ──
# Stub `glab` piloté par env, même discipline que le stub `gh` ci-dessus : log complet dans
# `$GL_LOG`, statut/erreurs pilotés par variable pour ne dépendre d'aucun vrai `glab auth`.
glbin="$T/glbin"; mkdir -p "$glbin"
cat > "$glbin/glab" <<'EOF'
#!/usr/bin/env bash
echo "glab $*" >> "$GL_LOG"
case "$*" in
  "mr view"*)                    exit 1 ;;
  "mr create"*)                  printf '%s\n' "$GL_MR_URL"; exit 0 ;;
  "ci status --wait")
      [ -n "${GL_WAIT_SLEEP:-}" ] && sleep "$GL_WAIT_SLEEP"
      [ -n "${GL_WAIT_STDERR:-}" ] && printf '%s\n' "$GL_WAIT_STDERR" >&2
      # Statut délibérément contradictoire sur stdout — il faut que le
      # verdict ignore ce flux et ne suive que l'API du pipeline de la MR.
      [ -n "${GL_CI_STATUS_STDOUT:-}" ] && printf '%s\n' "$GL_CI_STATUS_STDOUT"
      exit "${GL_WAIT_RC:-0}" ;;
  *"merge_requests/"*"/pipelines")
      # `glab api` n'a pas de `--jq` (flag inexistant, testé en direct sur
      # `glab 1.116.0` — `ERROR: Unknown flag: --jq`) : `bulk_commit.sh` filtre
      # désormais lui-même via `jq` sur la sortie JSON brute. Les 3 anciens appels
      # (`length`/`.[0].status`/`.[0].id`) tapent tous ce même endpoint sans rien
      # qui les distingue — le stub rend donc un unique document complet, piloté
      # par les mêmes variables qu'avant.
      if [ -n "${GL_PIPE_COUNT_SEQ_FILE:-}" ] && [ -s "$GL_PIPE_COUNT_SEQ_FILE" ]; then
          n="$(head -1 "$GL_PIPE_COUNT_SEQ_FILE")"
          tail -n +2 "$GL_PIPE_COUNT_SEQ_FILE" > "$GL_PIPE_COUNT_SEQ_FILE.tmp"
          mv "$GL_PIPE_COUNT_SEQ_FILE.tmp" "$GL_PIPE_COUNT_SEQ_FILE"
      else
          n="${GL_PIPE_COUNT:-1}"
      fi
      if [ "$n" = "0" ]; then printf '[]\n'; exit 0; fi
      if [ -n "${GL_STATUS_SEQ_FILE:-}" ] && [ -s "$GL_STATUS_SEQ_FILE" ]; then
          s="$(head -1 "$GL_STATUS_SEQ_FILE")"
          tail -n +2 "$GL_STATUS_SEQ_FILE" > "$GL_STATUS_SEQ_FILE.tmp"
          mv "$GL_STATUS_SEQ_FILE.tmp" "$GL_STATUS_SEQ_FILE"
      else
          s="${GL_STATUS-}"
      fi
      id="${GL_PIPE_ID:-1}"
      # `id` toujours quoté (JSON string valide même si `GL_PIPE_ID='-'`, gl-canceled,
      # simule un id inexploitable) : `jq -r '.id'` le rend tel quel, le
      # `grep -oE '[0-9]+'` côté script ne matche alors rien — même résultat qu'avant
      # (log_path `-`) — sans casser le parse JSON du `status` voisin dans le même document.
      if [ -z "$s" ]; then
          printf '[{"id": "%s", "status": null}]\n' "$id"
      else
          printf '[{"id": "%s", "status": "%s"}]\n' "$id" "$s"
      fi
      exit 0 ;;
  *"pipelines/"*"/jobs")
      if [ -n "${GL_JOB_ID:-}" ]; then
          printf '[{"id": "%s", "status": "%s"}]\n' "$GL_JOB_ID" "${GL_JOB_STATUS:-failed}"
      else
          printf '[]\n'
      fi
      exit 0 ;;
  *"jobs/"*"/trace")
      [ -n "${GL_TRACE:-}" ] && printf '%s\n' "$GL_TRACE"
      exit 0 ;;
esac
exit 0
EOF
chmod +x "$glbin/glab"

mk_watch_repo_gitlab() {   # NAME → repo poussé sur `main`, checkout feature branch, remote "gitlab"
    local n="$1" b="$T/gitlab-$1-origin.git"
    git init -q --bare -b main "$b"
    mkdir -p "$T/$n" && git -C "$T/$n" init -q -b main
    git -C "$T/$n" remote add origin "$b"
    git -C "$T/$n" commit -q --allow-empty -m init
    git -C "$T/$n" push -q -u origin main
    git -C "$T/$n" remote set-head origin main
    git -C "$T/$n" checkout -q -b "feature/$n"
    printf 'def x():\n    return 1\n' > "$T/$n/mod_$n.py"
}

run_watch_gl() {   # NAME  KEY=VAL...  → stdout de `apply --watch-ci` sur ce repo GitLab
    local n="$1"; shift
    local plan fp
    plan="$("$SCRIPT" plan --root "$T" --repo "$n" 2>&1)"
    fp="$(printf '%s' "$plan" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
    printf '%s\t✨ feat: %s\n' "$fp" "$n" > "$T/m_$n.tsv"
    env "$@" PATH="$glbin:$PATH" \
        "$SCRIPT" apply --root "$T" --repo "$n" --messages "$T/m_$n.tsv" --watch-ci 2>&1
}

# 6 lignes du mapping — `--wait` réussit direct, `_gl_status` rend le
# statut terminal du premier coup.
mk_watch_repo_gitlab gl-pass
outglp="$(run_watch_gl gl-pass GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-pass/-/merge_requests/11' \
    GL_PIPE_COUNT=1 GL_STATUS=success BULK_CI_TIMEOUT=3)"
check "watch-ci gitlab: success -> pass" "$(ci_block "$outglp" | cut -f1-3)" \
    "gl-pass${tab}pass${tab}https://gitlab.example.com/g/gl-pass/-/merge_requests/11"

mk_watch_repo_gitlab gl-fail
outglf="$(run_watch_gl gl-fail GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-fail/-/merge_requests/12' \
    GL_PIPE_COUNT=1 GL_STATUS=failed GL_PIPE_ID=55 GL_JOB_ID=77 GL_TRACE='job failed: exit 1' BULK_CI_TIMEOUT=3)"
glfb="$(ci_block "$outglf")"
check "watch-ci gitlab: failed -> fail" "$(printf '%s' "$glfb" | cut -f1-2)" "gl-fail${tab}fail"
check "watch-ci gitlab: fail carries a non-empty log-extract path" \
    "$([ -s "$(printf '%s' "$glfb" | cut -f4)" ] && echo yes || echo no)" yes

mk_watch_repo_gitlab gl-canceled
outglc="$(run_watch_gl gl-canceled GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-canceled/-/merge_requests/13' \
    GL_PIPE_COUNT=1 GL_STATUS=canceled GL_PIPE_ID='-' BULK_CI_TIMEOUT=3)"
check "watch-ci gitlab: canceled -> fail" "$(ci_block "$outglc" | cut -f1-2)" "gl-canceled${tab}fail"

# Contrepartie de `gl-manual` ci-dessous : `skipped` sans job manuel (aucun
# `GL_JOB_ID`) reste `no-ci` — seule la présence d'un job `status: manual`
# doit faire basculer un pipeline `skipped` en `pass`.
mk_watch_repo_gitlab gl-skipped
outgls="$(run_watch_gl gl-skipped GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-skipped/-/merge_requests/14' \
    GL_PIPE_COUNT=1 GL_STATUS=skipped BULK_CI_TIMEOUT=3)"
check "watch-ci gitlab: skipped, no manual job -> no-ci" "$(ci_block "$outgls")" "gl-skipped${tab}no-ci${tab}https://gitlab.example.com/g/gl-skipped/-/merge_requests/14${tab}-"

# GitLab n'expose jamais `status: "manual"` au niveau pipeline pour un gate
# manuel jamais déclenché — confirmé sur witness réel (repo jetable
# `pinky-ci-watch-witness`, MR `!3`) : le pipeline vaut `skipped`, seul le job
# porte `status: "manual"`. Stub aligné sur cette réalité, pas sur l'hypothèse
# initiale (fausse) d'un `status` pipeline littéralement `manual`.
mk_watch_repo_gitlab gl-manual
outglm="$(run_watch_gl gl-manual GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-manual/-/merge_requests/15' \
    GL_PIPE_COUNT=1 GL_STATUS=skipped GL_PIPE_ID=88 GL_JOB_ID=89 GL_JOB_STATUS=manual BULK_CI_TIMEOUT=3)"
check "watch-ci gitlab: skipped + job manuel -> pass (choix de process, pas un échec CI)" \
    "$(ci_block "$outglm" | cut -f1-2)" "gl-manual${tab}pass"

mk_watch_repo_gitlab gl-timeout
outglt="$(run_watch_gl gl-timeout GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-timeout/-/merge_requests/16' \
    GL_PIPE_COUNT=1 GL_STATUS=running BULK_CI_TIMEOUT=1 BULK_CI_GRACE=0 BULK_CI_POLL_INTERVAL=1)"
check "watch-ci gitlab: non-terminal past BULK_CI_TIMEOUT -> timeout" \
    "$(ci_block "$outglt" | cut -f1-2)" "gl-timeout${tab}timeout"

# `ci status --wait` peut mentir sur son propre stdout (aucune garantie
# GitLab dessus) — le verdict doit suivre exclusivement le pipeline de la MR via
# `_gl_status`, jamais ce flux.
mk_watch_repo_gitlab gl-wait-contradicts
outglwc="$(run_watch_gl gl-wait-contradicts GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-wait-contradicts/-/merge_requests/23' \
    GL_PIPE_COUNT=1 GL_STATUS=success GL_CI_STATUS_STDOUT=failed BULK_CI_TIMEOUT=3)"
check "verdict suit le pipeline de la MR, jamais le stdout de ci status --wait" \
    "$(ci_block "$outglwc" | cut -f1-2)" "gl-wait-contradicts${tab}pass"

# `no-ci` tranché AVANT tout wait — liste de pipelines vide confirmée,
# aucun `ci status --wait` appelé.
mk_watch_repo_gitlab gl-noci
: > "$T/gl.log"
outglnc="$(run_watch_gl gl-noci GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-noci/-/merge_requests/17' \
    GL_PIPE_COUNT=0 BULK_CI_TIMEOUT=3)"
check "watch-ci gitlab: empty pipeline list -> no-ci" "$(ci_block "$outglnc")" \
    "gl-noci${tab}no-ci${tab}https://gitlab.example.com/g/gl-noci/-/merge_requests/17${tab}-"
check "watch-ci gitlab: no-ci never calls ci status --wait" \
    "$(grep -c 'glab ci status --wait' "$T/gl.log")" 0

# Re-probe de grâce (review bloquante #1) : le pipeline de MR n'apparaît qu'au
# second appel (course post-`mr create`, décisive sous Merged Results Pipelines
# #1017) -> jamais `no-ci`, le watch doit continuer et voir le vrai verdict.
mk_watch_repo_gitlab gl-grace-recovers
pcseqf="$T/gl-pipecount-seq.txt"; printf '0\n1\n' > "$pcseqf"
outglgr="$(run_watch_gl gl-grace-recovers GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-grace-recovers/-/merge_requests/26' \
    GL_PIPE_COUNT_SEQ_FILE="$pcseqf" GL_STATUS=success BULK_CI_TIMEOUT=3 BULK_CI_GRACE=0)"
check "no-ci grace re-probe: un pipeline qui apparaît après coup n'est jamais no-ci" \
    "$(ci_block "$outglgr" | cut -f1-2)" "gl-grace-recovers${tab}pass"

# `iid` illisible (URL de forme inattendue) -> `timeout`, jamais `no-ci` (review
# importante #2 : `no-ci` est traité comme `pass` côté parent, un cas non
# tranché ne doit jamais y atterrir).
mk_watch_repo_gitlab gl-bad-url
outglbu="$(run_watch_gl gl-bad-url GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-bad-url/-/nothing-here' \
    BULK_CI_TIMEOUT=3)"
check "unparseable MR URL (no iid) -> timeout, never no-ci" \
    "$(ci_block "$outglbu" | cut -f1-2)" "gl-bad-url${tab}timeout"

# Négatif : un appel de comptage qui échoue rend une chaîne VIDE, pas
# "0" — ce cas ne doit jamais dégrader en `no-ci` silencieux, sinon un GitLab
# injoignable serait faussement rapporté comme "pas de CI".
mk_watch_repo_gitlab gl-count-ambiguous
: > "$T/gl.log"
outglca="$(run_watch_gl gl-count-ambiguous GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-count-ambiguous/-/merge_requests/24' \
    GL_STATUS='' BULK_CI_TIMEOUT=1 BULK_CI_GRACE=0 BULK_CI_POLL_INTERVAL=1)"
check "un comptage ambigu (chaîne vide) n'est jamais traité comme no-ci" \
    "$(ci_block "$outglca" | cut -f1-2)" "gl-count-ambiguous${tab}timeout"
check "un comptage ambigu attend quand même (appelle ci status --wait)" \
    "$(grep -c 'glab ci status --wait' "$T/gl.log")" 1

# Négatif : job `failed` introuvable -> log_path = `-`, jamais un chemin.
mk_watch_repo_gitlab gl-fail-nolog
outglfn="$(run_watch_gl gl-fail-nolog GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-fail-nolog/-/merge_requests/18' \
    GL_PIPE_COUNT=1 GL_STATUS=failed GL_PIPE_ID=99 BULK_CI_TIMEOUT=3)"
check "watch-ci gitlab: fail without a recoverable job -> log_path -" \
    "$(ci_block "$outglfn" | cut -f4)" "-"

# Erreur `glab` ambiguë (statut jamais terminal, `--wait` sans le
# marqueur « unknown flag ») -> jamais `pass`, verdict `timeout`.
mk_watch_repo_gitlab gl-ambiguous
outgla="$(run_watch_gl gl-ambiguous GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-ambiguous/-/merge_requests/19' \
    GL_PIPE_COUNT=1 GL_STATUS='' GL_WAIT_RC=1 BULK_CI_TIMEOUT=1 BULK_CI_GRACE=0 BULK_CI_POLL_INTERVAL=1)"
check "ambiguous glab error never yields pass" \
    "$(ci_block "$outgla" | cut -f1-2)" "gl-ambiguous${tab}timeout"

# Miroir de `kilo` côté GitHub : `--wait` hang au-delà du budget,
# `_bounded_cmd` le tue -> `timeout`, jamais un hang réel du script de test.
mk_watch_repo_gitlab gl-wait-hang
outglwh="$(run_watch_gl gl-wait-hang GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-wait-hang/-/merge_requests/25' \
    GL_PIPE_COUNT=1 GL_WAIT_SLEEP=4 GL_STATUS=running BULK_CI_TIMEOUT=1 BULK_CI_GRACE=0 BULK_CI_POLL_INTERVAL=1)"
check "watch-ci gitlab: --wait hang past BULK_CI_TIMEOUT is killed -> timeout" \
    "$(ci_block "$outglwh" | cut -f1-2)" "gl-wait-hang${tab}timeout"

# Poll fallback (`--wait` cassé/indisponible, « unknown flag ») : converge vers un
# statut terminal via le poll, jamais bloqué à `timeout` par construction.
mk_watch_repo_gitlab gl-fallback
seqf="$T/gl-status-seq.txt"; printf 'running\nrunning\nsuccess\n' > "$seqf"
outglfb="$(run_watch_gl gl-fallback GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-fallback/-/merge_requests/21' \
    GL_PIPE_COUNT=1 GL_WAIT_STDERR='unknown flag: --wait' GL_STATUS_SEQ_FILE="$seqf" \
    BULK_CI_TIMEOUT=5 BULK_CI_GRACE=0 BULK_CI_POLL_INTERVAL=0)"
check "poll fallback converges past 'running' to the real terminal status" \
    "$(ci_block "$outglfb" | cut -f1-2)" "gl-fallback${tab}pass"

# Avec `--no-watch-ci`, aucun appel `glab ci`/`glab api`, pas de bloc CI
# (le watch est ON par défaut ; `--no-watch-ci` est l'opt-out).
mk_watch_repo_gitlab gl-nowatch
: > "$T/gl.log"
plan_gnw="$("$SCRIPT" plan --root "$T" --repo gl-nowatch 2>&1)"
fp_gnw="$(printf '%s' "$plan_gnw" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t✨ feat: gl-nowatch\n' "$fp_gnw" > "$T/m_gl-nowatch.tsv"
out_gnw="$(env GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.example.com/g/gl-nowatch/-/merge_requests/22' \
    PATH="$glbin:$PATH" "$SCRIPT" apply --root "$T" --repo gl-nowatch --messages "$T/m_gl-nowatch.tsv" --no-watch-ci 2>&1)"
check "--no-watch-ci (gitlab) -> no === CI block" "$(printf '%s' "$out_gnw" | grep -c '^=== CI ')" 0
check "--no-watch-ci (gitlab) -> no glab ci/api call" \
    "$(grep -cE 'glab (ci|api)' "$T/gl.log")" 0

# `_mr_iid` insensible au nombre de (sous-)groupes et au host — le
# `iid` réellement transmis à l'API est vérifié dans le log du stub, pas deviné.
mk_watch_repo_gitlab gl-iid-simple
: > "$T/gl.log"
run_watch_gl gl-iid-simple GL_LOG="$T/gl.log" GL_MR_URL='https://gitlab.com/group/gl-iid-simple/-/merge_requests/101' \
    GL_PIPE_COUNT=1 GL_STATUS=success BULK_CI_TIMEOUT=3 >/dev/null
# 2 appels attendus (count + status), même iid réutilisé sur les deux — pas 1.
check "iid parsed from a single-group URL" \
    "$(grep -c 'merge_requests/101/pipelines' "$T/gl.log")" 2

mk_watch_repo_gitlab gl-iid-subgroup
: > "$T/gl.log"
run_watch_gl gl-iid-subgroup GL_LOG="$T/gl.log" \
    GL_MR_URL='https://gitlab.com/group/subgroup/gl-iid-subgroup/-/merge_requests/202' \
    GL_PIPE_COUNT=1 GL_STATUS=success BULK_CI_TIMEOUT=3 >/dev/null
check "iid parsed from a nested-subgroup URL" \
    "$(grep -c 'merge_requests/202/pipelines' "$T/gl.log")" 2

mk_watch_repo_gitlab gl-iid-host
: > "$T/gl.log"
run_watch_gl gl-iid-host GL_LOG="$T/gl.log" \
    GL_MR_URL='https://git.example.com/g/sub1/sub2/gl-iid-host/-/merge_requests/303' \
    GL_PIPE_COUNT=1 GL_STATUS=success BULK_CI_TIMEOUT=3 >/dev/null
check "iid parsed on a custom host, deep subgroup path" \
    "$(grep -c 'merge_requests/303/pipelines' "$T/gl.log")" 2

# ════════════════════════════════════════════════════════════════════════════
# Watch CI par défaut + recovery push-conflict
# ════════════════════════════════════════════════════════════════════════════

# ── Les 3 branches de push_with_recovery, oracle sur la ligne RESULTS ──
# Chaque cas : un repo sur `feature/X` dont `origin/feature/X` a divergé. `--no-pr`
# pour que l'état RESULTS soit celui décidé par le push, sans dépendre d'un stub `gh`.

rline() { printf '%s' "$1" | awk '/^=== RESULTS/{f=1;next} /^=== END RESULTS/{f=0} f'; }

diverge_origin() {   # NAME  CONTENT  → pousse CONTENT dans f.txt sur origin/feature/NAME
    local n="$1" content="$2" c
    c="$(mktemp -d)"; git clone -q "$T/$n-origin.git" "$c/c"
    # `-B … origin/…` : reprend la branche distante EXISTANTE (le HEAD du bare pointe
    # `main`, non né → `git clone` ne checkoute rien ; un `checkout -b` créerait une
    # branche orpheline dont le push divergerait au lieu d'avancer origin).
    git -C "$c/c" checkout -q -B "feature/$n" "origin/feature/$n"
    printf '%s' "$content" > "$c/c/f.txt"
    git -C "$c/c" -c user.email=t@t -c user.name=t add -A
    git -C "$c/c" -c user.email=t@t -c user.name=t commit -q -m "origin-side $n"
    git -C "$c/c" push -q origin "feature/$n"
    rm -rf "$c"
}

mk_pw_repo() {   # NAME  → repo poussé sur feature/NAME, avec un f.txt local à committer
    local n="$1" b="$T/$1-origin.git"
    git init -q --bare -b main "$b"
    mkdir -p "$T/$n" && git -C "$T/$n" init -q -b main
    git -C "$T/$n" remote add origin "$b"
    printf 'base\n' > "$T/$n/f.txt"
    git -C "$T/$n" -c user.email=t@t -c user.name=t add -A
    git -C "$T/$n" -c user.email=t@t -c user.name=t commit -q -m init
    git -C "$T/$n" checkout -q -b "feature/$n"
    git -C "$T/$n" push -q -u origin "feature/$n"
    git -C "$T/$n" remote set-head origin main 2>/dev/null || true
}

run_pw() {   # NAME  → apply --no-pr --no-watch-ci sur ce repo, rend stdout
    local n="$1" plan fp
    plan="$("$SCRIPT" plan --root "$T" --repo "$n" 2>&1)"
    fp="$(printf '%s' "$plan" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
    printf '%s\t✨ feat: %s\n' "$fp" "$n" > "$T/m_pw_$n.tsv"
    "$SCRIPT" apply --root "$T" --repo "$n" --messages "$T/m_pw_$n.tsv" --no-pr --no-watch-ci 2>&1
}

# (a) rebase propre + retry OK → poussé (pushed-nopr). L'origin ajoute une ligne en
# tête de f.txt, le local en ajoute une en queue — zones disjointes, rebase sans conflit.
mk_pw_repo pw-ok
diverge_origin pw-ok $'ORIGIN\nbase\n'
printf 'base\nLOCAL\n' > "$T/pw-ok/f.txt"
out_pwok="$(run_pw pw-ok)"
check "push_with_recovery (a): rebase clean + retry OK -> pushed-nopr" \
    "$(rline "$out_pwok")" "pw-ok${tab}pushed-nopr${tab}-"

# (b) rebase propre mais le retry échoue (hook pre-receive qui refuse) → commit-nopush.
mk_pw_repo pw-retryfail
diverge_origin pw-retryfail $'ORIGIN\nbase\n'
printf 'base\nLOCAL\n' > "$T/pw-retryfail/f.txt"
hookf="$T/pw-retryfail-origin.git/hooks/pre-receive"
printf '#!/bin/sh\nexit 1\n' > "$hookf"; chmod +x "$hookf"
out_pwrf="$(run_pw pw-retryfail)"
check "push_with_recovery (b): rebase clean + retry fails -> commit-nopush" \
    "$(rline "$out_pwrf")" "pw-retryfail${tab}commit-nopush${tab}-"

# (c) rebase en conflit réel → push-conflict, colonne 3 = chemin (jamais une URL),
# le fichier au chemin liste les fichiers unmerged, le repo est laissé propre.
mk_pw_repo pw-conflict
diverge_origin pw-conflict $'THEIRS\n'
printf 'OURS\n' > "$T/pw-conflict/f.txt"
out_pwc="$(run_pw pw-conflict)"
pwc_line="$(rline "$out_pwc")"
check "push_with_recovery (c): real rebase conflict -> push-conflict state" \
    "$(printf '%s' "$pwc_line" | cut -f1-2)" "pw-conflict${tab}push-conflict"
pwc_path="$(printf '%s' "$pwc_line" | cut -f3)"
check "push-conflict col 3 is a filesystem path, not a URL" \
    "$([ "${pwc_path#/}" != "$pwc_path" ] && ! printf '%s' "$pwc_path" | grep -q '://' && echo yes || echo no)" yes
check "push-conflict col 3 file lists the unmerged path (f.txt)" \
    "$(grep -c '^f\.txt$' "$pwc_path")" 1
check "push-conflict leaves the repo clean (no rebase in progress)" \
    "$([ -d "$T/pw-conflict/.git/rebase-merge" ] || [ -d "$T/pw-conflict/.git/rebase-apply" ] && echo dirty || echo clean)" clean
check "push-conflict keeps the local commit (HEAD advanced past init)" \
    "$(git -C "$T/pw-conflict" log --oneline | wc -l | tr -d ' ')" 2

# ── Garde du skill `commit` : `--no-pr` force watch off — pas de bloc
# `=== CI` vide même sans `--no-watch-ci` (le skill `commit` appelle toujours `--no-pr`).
mk_watch_repo papa
plan_p="$("$SCRIPT" plan --root "$T" --repo papa 2>&1)"
fp_p="$(printf '%s' "$plan_p" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t✨ feat: papa\n' "$fp_p" > "$T/m_papa.tsv"
outp="$(env GH_REPO=papa PATH="$cibin:$PATH" GH_CI_COUNT=2 BULK_CI_TIMEOUT=3 \
    "$SCRIPT" apply --root "$T" --repo papa --messages "$T/m_papa.tsv" --no-pr 2>&1)"
check "--no-pr suppresses the CI block (no empty === CI)" \
    "$(printf '%s' "$outp" | grep -c '^=== CI ')" 0

# ── watch CI ON par défaut (aucun flag) → bloc === CI présent ──
mk_watch_repo oscar
plan_o="$("$SCRIPT" plan --root "$T" --repo oscar 2>&1)"
fp_o="$(printf '%s' "$plan_o" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t✨ feat: oscar\n' "$fp_o" > "$T/m_oscar.tsv"
outo="$(env GH_REPO=oscar PATH="$cibin:$PATH" GH_CI_COUNT=2 GH_CI_WATCH_RC=0 BULK_CI_TIMEOUT=3 \
    "$SCRIPT" apply --root "$T" --repo oscar --messages "$T/m_oscar.tsv" 2>&1)"
check "apply with NO flag -> === CI block present" \
    "$(printf '%s' "$outo" | grep -c '^=== CI ')" 1
check "default watch reaches a verdict" \
    "$(ci_block "$outo" | cut -f1-2)" "oscar${tab}pass"

# ── Watches en lots parallèles, oracle de concurrence ──
# 6 repos, BULK_CI_PARALLEL=3 → au plus 3 watches simultanés, 2 lots. Chaque appel
# `pr checks --watch` du stub inscrit "start <pid>" / "end <pid>" dans un journal ;
# on reconstruit le nb max de watches actifs en parallèle. Pas d'assertion wall-clock.
PROOT="$(mktemp -d)"
concbin="$T/concbin"; mkdir -p "$concbin"
cat > "$concbin/gh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "pr view"*)   exit 1 ;;
  "pr create"*) echo "https://github.com/x/${GH_REPO}/pull/1"; exit 0 ;;
  *"pr checks"*"--json state"*) echo "1"; exit 0 ;;
  *"pr checks"*"--watch"*)
      echo "start $$" >> "$CONC_LOG"
      sleep 0.5
      echo "end $$" >> "$CONC_LOG"
      exit 0 ;;
esac
exit 0
EOF
chmod +x "$concbin/gh"
export CONC_LOG="$T/conc.log"; : > "$CONC_LOG"
for n in c1 c2 c3 c4 c5 c6; do
    b="$PROOT/$n-origin.git"; git init -q --bare -b main "$b"
    mkdir -p "$PROOT/$n" && git -C "$PROOT/$n" init -q -b main
    git -C "$PROOT/$n" remote add origin "$b"
    git -C "$PROOT/$n" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
    git -C "$PROOT/$n" push -q -u origin main
    git -C "$PROOT/$n" remote set-head origin main
    git -C "$PROOT/$n" checkout -q -b "feature/$n"
    printf 'def x():\n    return 1\n' > "$PROOT/$n/mod_$n.py"
done
planc="$("$SCRIPT" plan --root "$PROOT" 2>&1)"
: > "$T/mc.tsv"
for fp in $(printf '%s' "$planc" | grep -o 'GROUP [a-f0-9]\{12\}' | cut -d' ' -f2 | sort -u); do
    printf '%s\t✨ feat: c\n' "$fp" >> "$T/mc.tsv"
done
env PATH="$concbin:$PATH" BULK_CI_PARALLEL=3 BULK_CI_TIMEOUT=5 \
    "$SCRIPT" apply --root "$PROOT" --messages "$T/mc.tsv" >/dev/null 2>&1
maxconc="$(awk '/^start/{c++; if(c>m)m=c} /^end/{c--} END{print m+0}' "$CONC_LOG")"
nstart="$(grep -c '^start' "$CONC_LOG")"
check "6 watches all ran" "$nstart" 6
check "concurrency capped at BULK_CI_PARALLEL (3)" \
    "$([ "$maxconc" -le 3 ] && [ "$maxconc" -ge 2 ] && echo ok || echo "got $maxconc")" ok
rm -rf "$PROOT"

# ── Hooks : `prek` tourne DANS le repo scanné, jamais dans le répertoire d'appel ──
HROOT="$(mktemp -d)"; hookbin="$HROOT/bin"; mkdir -p "$hookbin"
mkdir -p "$HROOT/hotel" && git -C "$HROOT/hotel" init -q -b develop
git -C "$HROOT/hotel" commit -q --allow-empty -m init
printf 'repos: []\n' > "$HROOT/hotel/.pre-commit-config.yaml"
printf 'change\n' > "$HROOT/hotel/README.md"
cat > "$hookbin/prek" <<HOOK
#!/usr/bin/env bash
pwd -P >> "$HROOT/prek_cwd"
HOOK
chmod +x "$hookbin/prek"
(cd / && PATH="$hookbin:$PATH" "$SCRIPT" plan --root "$HROOT" --repo hotel >/dev/null 2>&1)
check "prek runs inside the scanned repo, not the caller's directory" \
    "$(head -1 "$HROOT/prek_cwd" 2>/dev/null)" "$(cd "$HROOT/hotel" && pwd -P)"
rm -rf "$HROOT"

[ "$fail" -eq 0 ] && echo "OK" || echo "ÉCHEC"
exit "$fail"
