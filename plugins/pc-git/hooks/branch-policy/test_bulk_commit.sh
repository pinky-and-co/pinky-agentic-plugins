#!/usr/bin/env bash
# Test du groupement de bulk_commit.sh — le comportement qui porte tout le gain.
#
# Ce qu'il verrouille : des repos recevant une distribution IDENTIQUE doivent tomber dans
# UN groupe, y compris quand l'un d'eux traînait un `.DS_Store`. Sans l'exclusion du
# `.gitignore` dans l'empreinte, ce repo-là sort du groupe et le lot se fragmente —
# c'est le bug trouvé au premier test réel, et c'est lui que ce fichier empêche de revenir.

set -uo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bulk_commit.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

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

check "5 repos detected"          "$(printf '%s' "$plan" | grep -c '^  /')"                    5
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
outfox="$(PATH="$ghbin:$PATH" "$SCRIPT" apply --root "$T" --repo foxtrot --messages "$T/mfox.tsv" 2>&1)"

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

# NFR-0023-18 : le run foxtrot ci-dessus n'a PAS `--watch-ci` — aucun bloc CI, et
# `gh pr checks` n'a jamais été appelé.
check "no --watch-ci -> no === CI block" \
    "$(printf '%s' "$outfox" | grep -c '^=== CI ')" 0
check "no --watch-ci -> gh pr checks never called" \
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
outg="$(PATH="$ghbin:$PATH" "$SCRIPT" apply --root "$T" --repo golf --messages "$T/mg.tsv" 2>&1)"
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

[ "$fail" -eq 0 ] && echo "OK" || echo "ÉCHEC"
exit "$fail"
