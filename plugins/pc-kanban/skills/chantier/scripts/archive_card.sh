#!/usr/bin/env bash
# Archive a done card: archive_card.sh <card-path>
# e.g. archive_card.sh _KANBAN/CHANTIER0022-migration-.../4-done/foo.md
# A reviewed card in 3-in-progress/ skips 4-done/ — the script refuses one without a checked
# reviewed box.
set -euo pipefail

card="$1"
[[ -f "$card" ]] || { echo "no such card: $card" >&2; exit 1; }

card_dir=$(dirname "$card")             # .../CHANTIER{NNNN}-<slug>/<column>
column=$(basename "$card_dir")
# 3-in-progress : archivage direct d'une card reviewée, sans étape intermédiaire en 4-done. La
# case reviewed cochée est exigée — l'audit des cards closes sans relecture
# (--list-unreviewed-done) ne voit que 4-done/, ce refus est le seul filet sur ce raccourci.
case "$column" in
  4-done) ;;
  3-in-progress)
    grep -q -- '- \[x\] reviewed (' "$card" || {
      echo "card in 3-in-progress/ has no checked reviewed box: $card" >&2
      echo "close it via 4-done, or check the reviewed box first (finishing-a-card.md)" >&2
      exit 1
    }
    ;;
  *)
    echo "card is not in a 4-done/ or 3-in-progress/ directory: $card" >&2
    exit 1
    ;;
esac

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
