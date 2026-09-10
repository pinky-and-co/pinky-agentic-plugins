# Reacting to a `push-conflict` — parent side

`commit-and-pr` runs in a fork. On a push rejection (non-fast-forward — the remote branch moved),
the fork tries to recover on its own: `fetch` + `rebase`, then one retry. It never touches conflict
markers itself. If the rebase comes back clean, the retry either succeeds silently or falls back to
the ordinary `commit-nopush` state — nothing for you to do differently there. `push-conflict` is the
one case left: the rebase hit a real conflict. The `=== RESULTS` line for that repo carries a file
path in the third column instead of a PR URL — a plain list of the conflicting paths
(`git diff --name-only --diff-filter=U`, captured right before the fork ran `rebase --abort`). The
repo itself is clean: no half-finished rebase, no unmerged index, the local commit sits exactly where
it was before the fork tried to push.

## Decide which kind before touching anything

Open the file at that path and read the list. For each conflicting file, the same split as CI
failures applies:

- **Dictated by the file.** A lockfile (`package-lock.json`, `uv.lock`, `Cargo.lock`), a generated
  artifact, a formatter's output — anything reproducible from a command rather than authored by
  hand. Fix: `git -C <repo> fetch origin <branch> && git -C <repo> rebase origin/<branch>`, resolve
  by regenerating the file (not by hand-editing the markers), `git add`, `git rebase --continue`,
  then re-run `commit-and-pr` on that repo so it re-pushes on top of the resolved history.
- **A judgment call.** Two changes landed on the same lines of hand-written logic, or the conflict
  spans more than a mechanical regeneration. **Stop.** Surface the file list and which repo, and
  hand back — do not guess at which side should win.

The line between the two is the same test as `ci-reaction.md`: can the resolution be read straight
off the file's own regeneration command, with no decision about intent? If yes, dictated. If you
have to reason about which change should survive, judgment.

The conflict-files list lives under `$TMPDIR` (or `/tmp`), same as the CI log extract. Nothing
deletes it — read it, then remove it once you have acted; leaving it is harmless but yours to clean.

## Never

- Resolve a hand-authored conflict without surfacing it — a silently-picked side is a silently
  discarded change.
- Re-run `commit-and-pr --watch-ci` on a repo still mid-rebase; the fork does not check for that
  state and will re-stage on top of an inconsistent tree.
- Report `push-conflict` as `commit-nopush` or vice versa — the file path only exists for the
  former, and treating them the same hides that a rebase was even attempted.
