---
name: apply-pattern
description: Replay a change already made on one witness target onto the remaining targets. The witness diff is the specification — this skill applies it, it never re-decides it. Use after doing a convention/refactor for real on one repo, to propagate it without pasting prompts into N sessions.
argument-hint: "<witness-repo> <target> [<target>…] — or <witness-repo> --rest"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(git log:*), Bash(git rev-parse:*), Bash(rg:*), Bash(find:*), Bash(${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh:*), Bash(jq:*), Bash(mktemp:*), Bash(gh:*), Bash(glab:*), Read, Edit, Write, Glob, Grep
---

# Apply a pattern to the remaining targets

The judgment already happened: someone made this change **for real**, on one target, at full tier.
Your job is the repetition, not the decision. If you find yourself deciding what the change should
be, stop and report — the pattern is not ready, and inventing it here produces N slightly different
answers at N different moments.

You run forked on `haiku` for exactly that reason. Cheap is correct here, because nothing is being
decided.

## Step 1 — Read the pattern

```
git -C <witness> diff HEAD
git -C <witness> diff HEAD~1   # if the witness change is already committed
```

That diff is the whole specification. Read it fully before touching anything.

Derive three things and write them down before proceeding:

- **what changed** — files, and within them, what kind of edit (a value, a section, a new file)
- **what is target-specific** — repo name, paths, module names: the parts that must differ
- **what must be identical** — the wording, the structure, the ordering

If the diff is empty, say so and stop.

## Step 2 — Apply, target by target

Sequentially. Do not spawn agents — you are already a fork, and a fork cannot fork.

For each target:

1. Read the corresponding file(s) **in that target** first. Never assume the witness's content is
   there: a target may already carry part of the change, or a variant.
2. Apply the equivalent edit, substituting only what Step 1 identified as target-specific.
3. Re-read what you wrote if the edit was structural.

**Three things you never do**, because each one turns a mechanical pass into a silent mess:

- **Never `git apply` the witness diff blindly.** Line numbers and surrounding content differ; a
  patch that applies cleanly to the wrong place is worse than one that fails.
- **Never fix anything else you notice.** Out-of-pattern problems get reported, not repaired. One
  pass, one change.
- ~~**Never commit.**~~ **New:** Commit and open a PR/MR for each successful target (✅ or ⚠️).
  Invoke `bulk_commit.sh apply` once per target with a commit message derived from the pattern
  (e.g., the witness's commit message, or a generic convention message). Capture the PR/MR URL
  from the script's `=== RESULTS` block (column `pr_url`). On failure or `❌` states, no commit.

## Step 2 (continued) — Commit and open PR/MR (if applied)

For each target where the pattern **was successfully applied** (✅ or ⚠️):

1. **Stage the applied changes** (required by `bulk_commit.sh`):
   ```bash
   git -C <target-path> add -A
   ```
   The commit script only reads **already-staged** changes; Edit/Write alone does not stage.

2. **Discover fingerprints and emit messages** (reuse the script's plan mode):
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh plan \
     --root "${CLAUDE_PROJECT_DIR}" --repo <target-repo-basename>
   ```
   Parse the `=== GROUP <fingerprint>` line(s) and collect all fingerprints in this target's diff.

3. **Derive commit messages** for each fingerprint (one message per unique diff group):
   - Use the witness commit's message, or
   - Generate a generic message from the pattern description (e.g., "Apply convention: <what changed>").
   Write to a temp file, one tab-separated line per fingerprint:
   ```
   <fingerprint1>\t<message1>
   <fingerprint2>\t<message2>
   …
   ```

4. **Invoke the commit + PR/MR script** (from `pc-git` plugin):
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh apply \
     --root "${CLAUDE_PROJECT_DIR}" \
     --repo <target-repo-basename> \
     --messages <temp-file>
   ```
   - **`--root`** must be a workspace root (a folder containing multiple git repos as direct
     subdirectories). Use `${CLAUDE_PROJECT_DIR}`, which is set by the Claude Code harness.
   - **`--repo`** is the basename of the target repo (the folder name, e.g., `pinky-core`, not the
     full path).

5. **Parse the `=== RESULTS` block** from the script's output. Each line is tab-separated:
   `<repo>\t<state>\t<pr_url_or_dash>`, where `state` ∈ {`pushed-pr`, `pushed-nopr`,
   `commit-nopush`, `pushed-prfailed`, `failed`, `skipped`}.
   - Store both state and URL: `results[<target>] = {"state": <state>, "url": <pr_url_or_dash>}`.
   - **Do NOT collapse or reinterpret states** — report the raw state verbatim. Each state
     carries distinct information (e.g., `commit-nopush` means commit succeeded locally but push
     failed; human must investigate; `pushed-prfailed` means commit+push succeeded but PR creation
     failed; human must open PR by hand; `failed`/`skipped` mean nothing happened).

6. **If the script exits nonzero**, record it as an error and continue to the next target.

## Step 3 — Report, per target (pattern outcome + script outcome)

Report **two dimensions** per target: did the pattern apply, and did the commit/PR succeed. One
line each, keeping states distinct:

**Pattern outcome** (from Step 2):

| State | Meaning |
|---|---|
| ✅ | applied, matches the pattern |
| ⚠️ | applied with a deviation — say which, in the same line |
| ⏭️ | already had the change, nothing to do |
| ❌ | not applied — the target diverges too much for the pattern to mean anything here |

**Script outcome** (from step 2 continued, when pattern was applied):

| Script state | Meaning | Next step |
|---|---|---|
| `pushed-pr` | commit + push + PR/MR created | PR is open, review/merge as usual |
| `pushed-nopr` | commit + push to integration branch (unguarded), or `--no-pr` flag | no PR needed |
| `commit-nopush` | commit succeeded, push failed | **human must investigate push failure** |
| `pushed-prfailed` | commit + push succeeded, PR creation failed | **human must open PR by hand** |
| `failed` | commit failed | changes remain staged, nothing lost |
| `skipped` | no message matched this diff group's fingerprint | **re-run plan + supply missing message** |

Format per line (pattern outcome first, then script state + URL):

```
<target> <pattern-state> <script-state> <pr_url_or_dash>
```

Example:

```
pinky-core ✅ pushed-pr https://github.com/pinky-and-co/pinky-core/pull/42
pinky-tools ⚠️ (deviation: comment format) pushed-pr https://github.com/pinky-and-co/pinky-tools/pull/15
pinky-ai ❌ - -
pinky-lib ✅ commit-nopush -
pinky-streamlit ✅ pushed-prfailed -
```

End with a summary and review commands:

```
=== Summary
5 targets: 2 PR created, 1 deviation + PR, 1 commit-failed, 1 push-failed, 1 not applied

=== Review each target diff (if needed)
git -C <target> diff        # per target (nothing committed on failed, staged on commit-nopush)
```

**Critical:** Do NOT flatten or omit the script states (`commit-nopush`, `pushed-prfailed`, etc.).
Each one signals distinct human actions:
- `commit-nopush` / `pushed-prfailed` → investigate, decide how to proceed
- `failed` / `skipped` → retry with fixes
- `❌ / ⏭️` → legitimate, no action needed

A run that claims success while hiding a `commit-nopush` or `pushed-prfailed` is worse than one that
reports it explicitly.

## Step 4 — Aggregate PR/MR URLs

After reporting all targets, aggregate the PR/MR URLs from step 2 (continued) for visibility:

```
=== PR/MR URLs (exhaustive)
<target> <state> <url_or_dash>
<target> <state> <url_or_dash>
…
```

This list appears in **two places** (doublet intentional):
1. **This terminal report** (the skill's output) — for immediate visibility in the session.
2. **`_AGENT_LOG/YYYY-MM-DD_<session>.md`** — auto-written by the harness Stop hook, durable and queryable for audits.

A target that failed commit/push/PR has `state` = `failed` and `url` = `-` (never omitted). This
helps distinguish "no PR opened" (explicit) from "no PR listed" (silent data loss).
