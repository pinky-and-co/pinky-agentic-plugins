#!/usr/bin/env bash
# Archive a done card: archive_card.sh <card-path>
# e.g. archive_card.sh _KANBAN/CHANTIER0022-migration-.../4-done/foo.md
set -euo pipefail

card="$1"
[[ -f "$card" ]] || { echo "no such card: $card" >&2; exit 1; }

card_dir=$(dirname "$card")             # .../CHANTIER{NNNN}-<slug>/4-done
[[ "$(basename "$card_dir")" == "4-done" ]] || {
  echo "card is not in a 4-done/ directory: $card" >&2
  exit 1
}

chantier_dir=$(dirname "$card_dir")     # .../CHANTIER{NNNN}-<slug>
kanban_root=$(dirname "$chantier_dir")  # .../_KANBAN

chantier_num=$(basename "$chantier_dir")
chantier_num="${chantier_num%%-*}"      # CHANTIER{NNNN}

dest_dir="$kanban_root/_ARCHIVE/${chantier_num}-done"
mkdir -p "$dest_dir"

if [[ ! -f "$dest_dir/.nav.yml" ]]; then
  cat > "$dest_dir/.nav.yml" <<'EOF'
sort:
  direction: desc
  by: filename
EOF
  echo "seeded $dest_dir/.nav.yml"
fi

dest="$dest_dir/$(date +%F)_$(basename "$card")"

if [[ -e "$dest" ]]; then
  echo "refusing to overwrite existing archive entry: $dest" >&2
  exit 1
fi

mv -- "$card" "$dest"
echo "archived -> $dest"
