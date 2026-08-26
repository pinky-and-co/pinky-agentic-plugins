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
for r in alpha bravo charlie; do mkrepo "$r"; printf 'shared\n' > "$T/$r/POLICY.md"; done
printf 'junk' > "$T/alpha/.DS_Store"          # bruit sur UN SEUL des trois
mkrepo delta; printf 'def f():\n    return 1\n' > "$T/delta/app.py"   # code → chemin gardé
mkrepo echo_; printf 'junk' > "$T/echo_/.DS_Store"                    # bruit et rien d'autre

plan="$("$SCRIPT" plan --root "$T" 2>&1)"

check "5 repos détectés"          "$(printf '%s' "$plan" | grep -c '^  /')"                    5
check "3 groupes distincts"       "$(printf '%s' "$plan" | grep -c '^=== GROUP')"              3
# Taille de chaque groupe (nombre de lignes de repo entre GROUP et END) : le groupe de la
# distribution partagée doit en compter 3. Compter par `grep -A<n>` ne marche pas — la
# fenêtre fixe tronque le groupe dès qu'il dépasse n lignes.
sizes="$(printf '%s\n' "$plan" | awk '/^=== GROUP/{n=0} /^  \//{n++} /^=== END/{print n}' | sort -rn | head -1)"
check "le bruit ne fragmente pas" "$sizes" 3
check "code → feature branch"     "$(printf '%s' "$plan" | grep 'delta' | grep -c 'feature-branch=yes')" 1
check "docs → commit direct"      "$(printf '%s' "$plan" | grep 'bravo' | grep -c 'feature-branch=no')"  1
check "groupe de bruit isolé"     "$(printf '%s' "$plan" | grep -c 'GROUP noise-gitignore')"   1
check ".DS_Store gitignoré"       "$(grep -c '^\.DS_Store$' "$T/alpha/.gitignore")"            1

# apply --dry-run ne doit rien committer, et doit couvrir les 5 repos.
printf '%s\t📝 docs: add POLICY\n' "$(printf '%s' "$plan" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)" > "$T/m.tsv"
dry="$("$SCRIPT" apply --root "$T" --messages "$T/m.tsv" --dry-run 2>&1)"
check "dry-run ne committe rien"  "$(git -C "$T/bravo" log --oneline | wc -l | tr -d ' ')"     1
check "groupe sans message → ⏭️"  "$(printf '%s' "$dry" | grep -c 'aucun message')"            1

# Une racine sans aucun dépôt n'est pas un workspace propre : c'est une racine fausse.
# Les confondre annonce "rien à faire" après avoir regardé au mauvais endroit — vu en vrai
# juste après un déplacement de workspace, avec 11 repos sales laissés au sol.
empty="$(mktemp -d)"
out="$("$SCRIPT" plan --root "$empty" 2>&1)"; rc=$?
rmdir "$empty"
check "racine vide → NO-REPOS-FOUND" "$(printf '%s' "$out" | grep -c 'NO-REPOS-FOUND')" 1
check "racine vide → exit non nul"   "$([ "$rc" -ne 0 ] && echo yes || echo no)"          yes
check "jamais NOTHING-TO-COMMIT"     "$(printf '%s' "$out" | grep -c 'NOTHING-TO-COMMIT')" 0

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
[ "$1 $2" = "pr view" ] && exit 1
exit 0
EOF
chmod +x "$ghbin/gh"
export GH_LOG="$T/gh.log"; : > "$GH_LOG"

planfox="$("$SCRIPT" plan --root "$T" --repo foxtrot 2>&1)"
fpfox="$(printf '%s' "$planfox" | grep -o 'GROUP [a-f0-9]\{12\}' | head -1 | cut -d' ' -f2)"
printf '%s\t✨ feat: add g\n' "$fpfox" > "$T/mfox.tsv"
PATH="$ghbin:$PATH" "$SCRIPT" apply --root "$T" --repo foxtrot --messages "$T/mfox.tsv" >/dev/null 2>&1

check "PR tentée depuis une feature branch pré-existante" \
    "$(grep -c '^gh pr create' "$GH_LOG")" 1
check "reste sur sa branche, aucune nouvelle créée" \
    "$(git -C "$T/foxtrot" branch --show-current)" "feature/preexisting"

[ "$fail" -eq 0 ] && echo "OK" || echo "ÉCHEC"
exit "$fail"
