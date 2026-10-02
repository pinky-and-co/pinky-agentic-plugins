#!/usr/bin/env bash
# Drop a card confirmed obsolete by the human: drop_card.sh [--keep-requirements] <card-path> <reason...>
# e.g. drop_card.sh _KANBAN/CHANTIER0025-.../1-todo/foo.md "superseded by bar, same need"
# Also retires from the chantier's story the requirements listed in the card's frontmatter
# (retire_requirements.py) — unless another open card still carries them. --keep-requirements leaves
# the story untouched: use it when the need survives the card ("same need", a replacement to come).
set -euo pipefail

keep_requirements=0
if [[ "${1:-}" == "--keep-requirements" ]]; then
  keep_requirements=1
  shift
fi

# Sous set -u, accéder à $1 sans argument échoue en "unbound variable" avant même les messages
# d'usage ci-dessous — le compte d'arguments doit être validé en premier.
[[ $# -ge 2 ]] || { echo "a card path and a reason are required — drop_card.sh [--keep-requirements] <card-path> <reason...>" >&2; exit 1; }

card="$1"
shift
reason="$*"

[[ -f "$card" ]] || { echo "no such card: $card" >&2; exit 1; }
[[ -n "$reason" ]] || { echo "a reason is required — drop_card.sh [--keep-requirements] <card-path> <reason...>" >&2; exit 1; }

card_dir=$(dirname "$card")
column=$(basename "$card_dir")
# 4-done n'est pas une colonne droppable : une card qui y est arrivée était déjà terminée, pas
# obsolète — c'est archive_card.sh qui la traite.
case "$column" in
  0-ideas|1-todo|2-selected-for-dev|3-in-progress) ;;
  *)
    echo "card is not in a droppable column (0-ideas/1-todo/2-selected-for-dev/3-in-progress): $card" >&2
    exit 1
    ;;
esac

chantier_dir=$(dirname "$card_dir")
kanban_root=$(dirname "$chantier_dir")

chantier_num=$(basename "$chantier_dir")
chantier_num="${chantier_num%%-*}"

# Dossier distinct de -done/ : un lecteur de _ARCHIVE/ ne doit jamais confondre travail terminé et
# idée abandonnée.
dest_dir="$kanban_root/_ARCHIVE/${chantier_num}-dropped"
mkdir -p "$dest_dir"

if [[ ! -f "$dest_dir/.nav.yml" ]]; then
  cat > "$dest_dir/.nav.yml" <<'EOF'
sort:
  direction: desc
  by: filename
EOF
  echo "seeded $dest_dir/.nav.yml"
fi

today=$(date +%F)
dest="$dest_dir/${today}_$(basename "$card")"

if [[ -e "$dest" ]]; then
  echo "refusing to overwrite existing archive entry: $dest" >&2
  exit 1
fi

# mv d'abord : si l'ajout de la raison échouait après coup, la card originale se retrouverait
# mutée sans avoir bougé — en la déplaçant avant d'écrire, un échec d'écriture laisse au pire une
# archive sans raison, jamais une card "en cours" avec une note "Dropped" trompeuse dedans.
mv -- "$card" "$dest"
printf '\n## Dropped (%s)\n\n%s\n' "$today" "$reason" >> "$dest"
echo "dropped -> $dest"

if [[ $keep_requirements -eq 1 ]]; then
  echo "requirements kept in the story (--keep-requirements)"
else
  # Même raisonnement que le mv : après lui, un échec laisse la story intacte, jamais l'inverse.
  here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  if ! python3 "$here/retire_requirements.py" "$dest" "$chantier_dir"; then
    echo "card dropped, but the story was not updated — re-run: python3 $here/retire_requirements.py $dest $chantier_dir" >&2
    exit 1
  fi
fi
