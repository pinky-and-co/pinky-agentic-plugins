#!/usr/bin/env bash
# Test du groupement de bulk_commit.sh — le comportement qui porte tout le gain.
#
# Ce qu'il verrouille : des repos recevant une distribution IDENTIQUE doivent tomber dans
# UN groupe, y compris quand l'un d'eux traînait un `.DS_Store`. Sans l'exclusion du
# `.gitignore` dans l'empreinte, ce repo-là sort du groupe et le lot se fragmente —
# c'est le bug trouvé au premier test réel, et c'est lui que ce fichier empêche de revenir.

set -uo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/bulk_commit.sh"
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

[ "$fail" -eq 0 ] && echo "OK" || echo "ÉCHEC"
exit "$fail"
