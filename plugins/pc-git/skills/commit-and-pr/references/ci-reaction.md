# Reacting to a `--watch-ci` verdict — parent side

`commit-and-pr --watch-ci` runs in a fork. The fork never fixes anything: it opens the PR/MR,
watches CI, and hands back one `=== CI` line per opened PR — `repo <TAB> verdict <TAB> url <TAB>
log_path|-`. This file is what **you, the parent session** do with each verdict. You have the code
context the fork does not.

## `pass`

Done. The PR is open and green. Nothing else to do for that repo.

## `no-ci`

The PR is open and the repo has no checks. Not a failure, not a pass — there is simply nothing to
wait for. Treat it like `pass`: the PR is open, hand back to the user.

## `fail` — decide which kind before touching anything

Read the extract at `log_path` (~80-line tail — GitHub: `gh run view --log-failed`; GitLab: the
failed job's `glab api .../trace`). If `log_path` is `-`, the watch saw a red check but the failing
run/job could not be fetched — open the PR/MR's checks (or pipeline) tab yourself before reacting;
do not guess from the check name alone.

- **Dictated by the tool.** The failing step names the file, the line, and the remedy —
  `ruff`, `mypy`, `mdformat`, `prettier`, a formatter's `--check`. The fix is mechanical and
  unambiguous. Apply it in the working tree, then re-run `commit-and-pr --watch-ci` on the same
  repo: it commits the fix onto the existing feature branch, pushes, and re-watches.
- **A judgment call.** A test fails, a build breaks, a type error that is not a one-line
  annotation, anything where "is this check stale or is my change wrong?" is a real question.
  **Stop.** Surface the extract to the user, say which repo and which check, and hand back. Do not
  guess.

The line between the two is: *can the fix be read straight off the tool's output with no decision?*
If yes, it is dictated. If you have to reason about intent, it is judgment.

The extract file lives under `$TMPDIR` (or `/tmp`). Nothing deletes it — read it, then remove it
once you have acted; leaving it is harmless (small, temp-dir) but yours to clean.

## `timeout`

CI did not finish within the watch window (`BULK_CI_TIMEOUT`, default 900s per PR). The PR is open;
the verdict is unknown, not bad. Give the user the URL and let them decide — wait longer, or check
the run by hand. Never report it as `pass` or `fail`.

On GitLab, `timeout` also covers an unreadable pipeline status (auth failure, project not found) —
nothing distinguishes that from "still running" without a human checking `glab auth status`. If the
PR/MR is on GitLab and `timeout` looks suspicious (e.g. every repo in the batch got it), check auth
before assuming it is just slow CI.

## Flaky

A check that fails, then passes on a plain re-run with no code change (a network blip, a runner
timeout, a transient registry 503). Re-run the check — `gh run rerun --failed` (GitHub) or `glab ci
retry <job-id>` (GitLab) on the run/job, or another `commit-and-pr --watch-ci` if the branch moved.
**Ceiling: 3 attempts, advisory.** There is no counter in the code and there will not be one; if a
check needs a 4th re-run it is not flaky, it is broken — treat it as a judgment-call `fail` and hand
back.

## Never

- Fix in the fork. The fork has `--watch-ci` to *report*, not to edit.
- Report `pass` for a repo whose `=== CI` line said otherwise.
- Flatten `no-ci` or `timeout` into `pass`.
