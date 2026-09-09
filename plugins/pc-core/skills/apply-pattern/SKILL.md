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
- ~~**Never commit.**~~ **New in FR-0025-23:** Commit and open a PR/MR for each successful target (✅
  or ⚠️). Invoke `bulk_commit.sh apply` once per target with a commit message derived from the
  pattern (e.g., the witness's commit message, or a generic convention message). Capture the PR/MR
  URL from the script's `=== RESULTS` block (column `pr_url`). On failure or `❌` states, no commit.

## Step 2.5 — Commit and open PR/MR (if applied)

For each target where the pattern **was successfully applied** (✅ or ⚠️):

1. **Derive a commit message** from the witness commit (if available) or use a generic description
   of the pattern.
2. **Invoke the commit + PR/MR script** (from `pc-git` plugin):
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/bulk_commit.sh apply \
     --root "${CLAUDE_PROJECT_DIR}" \
     --repo <target-repo-basename> \
     --messages <(echo -e "<fingerprint>\t<commit-message>")
   ```
   - **`--root`** must be a workspace root (a folder containing multiple git repos as direct
     subdirectories). Use `${CLAUDE_PROJECT_DIR}`, which is set by the Claude Code harness.
   - **`--repo`** is the basename of the target repo (the folder name, e.g., `pinky-core`, not the
     full path).
   - The script outputs a `=== RESULTS` block with one tab-separated line per repo:
     `<repo>\t<state>\t<pr_url>`, where `state` ∈ {`pushed-pr`, `pushed-nopr`, `commit-nopush`,
     `pushed-prfailed`, `failed`, `skipped`}.
3. **Parse the `pr_url` column** from the results and the `state` column. Store both:
   `results[<target>] = {"state": <state>, "url": <pr_url_or_dash>}`.
4. **If the script fails** (nonzero exit), record it as `state: failed` with `url: -` and continue
   to the next target.

## Step 3 — Report, per target

One line each, with **status + PR/MR URL** (new in FR-0025-23). States:

| State | Meaning | PR URL |
|---|---|---|
| ✅ | applied, matches the pattern | URL from step 2.5 (`pushed-pr`) or `-` if commit failed |
| ⚠️ | applied with a deviation — say which | URL from step 2.5 or `-` if commit failed |
| ⏭️ | already had the change, nothing to do | `-` (no commit attempted) |
| ❌ | not applied — pattern mismatch | `-` (no commit attempted) |

Format per line:

```
<target> <state> <url_or_dash>
```

Example:

```
pinky-core ✅ https://github.com/pinky-and-co/pinky-core/pull/42
pinky-tools ⚠️ (deviation: comment format) https://github.com/pinky-and-co/pinky-tools/pull/15
pinky-ai ❌ -
```

End with a summary count and the commands to review the diffs:

```
=== Summary
3 targets: 1 PR created, 1 PR created (deviation), 0 skipped, 1 not applied

=== Review each target diff (if needed, before they are merged)
git -C <target> diff        # per target
```

❌ and ⏭️ are legitimate outcomes, not failures to hide. A target where the pattern does not fit is
information the person needs; forcing it there is how a batch quietly corrupts a repo.

## Step 4 — Aggregate PR/MR URLs (FR-0025-23)

After reporting all targets, aggregate the PR/MR URLs from step 2.5 for visibility:

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

The card `chantier 0025/worker-remonte-pr-links` (FR-0025-23) depends on this reporting. See
`story.md` § « Décisions actées via grill (session 2026-09-09, 3ᵉ passe) ».
