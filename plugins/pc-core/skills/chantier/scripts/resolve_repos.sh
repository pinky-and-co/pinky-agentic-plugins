#!/usr/bin/env bash
# resolve_repos.sh — org/repo slug -> local absolute path, by matching `git remote get-url origin`.
#
# A directory basename collides across orgs; the remote does not (same identity kanban cards use
# for `repos:`, skill `kanban`). Bounded discovery reuses the same shape as
# hooks/branch-policy/bulk_commit.sh::discover_repos — one `.git` scan, not reinvented per caller.
#
# Usage: resolve_repos.sh <workspace-root> <org/repo> [<org/repo>...]
# Prints one absolute path per requested slug, in order. 0 or >1 local clones for a slug is
# reported on stderr and makes the exit code nonzero — never guessed.
set -euo pipefail

root="${1:?usage: resolve_repos.sh <workspace-root> <org/repo>...}"
shift
[ "$#" -ge 1 ] || { echo "usage: resolve_repos.sh <workspace-root> <org/repo>..." >&2; exit 2; }

normalize() {
    printf '%s' "$1" | sed -E 's#^(git@|https://|http://)[^:/]+[:/]##; s#\.git$##'
}

# Plain parallel arrays, not `declare -A` — the default macOS bash (3.2) has no associative
# arrays, and this repo's other bulk script (bulk_commit.sh) already avoids them for the same
# reason.
slugs=()
paths=()
while IFS= read -r git_dir; do
    repo="$(dirname "$git_dir")"
    url="$(git -C "$repo" remote get-url origin 2>/dev/null || true)"
    [ -n "$url" ] || continue
    slugs+=("$(normalize "$url")")
    paths+=("$repo")
done < <(find "$root" -maxdepth 4 -name .git -not -path '*/node_modules/*' -print 2>/dev/null)

status=0
for wanted in "$@"; do
    matches=()
    for i in "${!slugs[@]}"; do
        [ "${slugs[$i]}" = "$wanted" ] && matches+=("${paths[$i]}")
    done
    case "${#matches[@]}" in
        0)
            echo "resolve_repos: no local clone found for '$wanted' under $root" >&2
            status=1
            ;;
        1)
            printf '%s\n' "${matches[0]}"
            ;;
        *)
            echo "resolve_repos: '$wanted' matches ${#matches[@]} local clones, ambiguous:" >&2
            printf '  %s\n' "${matches[@]}" >&2
            status=1
            ;;
    esac
done

exit "$status"
