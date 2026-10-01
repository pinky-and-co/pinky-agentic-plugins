#!/usr/bin/env bash
# gain.sh — shows RTK's token-savings summary. Read-only, idempotent.
#
# Only the read options of `rtk gain` pass through; every other argument is refused without
# calling rtk, so a mutating option (`--reset`, `--yes`, or one added later) can never wipe stats.

set -euo pipefail

if ! command -v rtk >/dev/null 2>&1; then
    printf 'rtk is not installed: run `brew install rtk`\n'
    exit 1
fi

refuse() {
    printf 'gain: refused %s — only read options are allowed (-p -g -H -q -t -d -w -m -a -f -F --recalls)\n' "$1"
    exit 1
}

args=("$@")

while [ "$#" -gt 0 ]; do
    case "$1" in
        -p | --project | -g | --graph | -H | --history | -q | --quota | -d | --daily \
            | -w | --weekly | -m | --monthly | -a | --all | -F | --failures | --recalls)
            ;;
        -t | --tier | -f | --format)
            [ "$#" -ge 2 ] || refuse "$1 without a value"
            case "$2" in -*) refuse "$1 $2" ;; esac
            shift
            ;;
        --tier=[!-]* | --format=[!-]*)
            ;;
        *)
            refuse "$1"
            ;;
    esac
    shift
done

exec rtk gain ${args[@]+"${args[@]}"}
