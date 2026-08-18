---
name: apply-pattern
description: Replay a change already made on one witness target onto the remaining targets. The witness diff is the specification — this skill applies it, it never re-decides it. Use after doing a convention/refactor for real on one repo, to propagate it without pasting prompts into N sessions.
argument-hint: "<witness-repo> <target> [<target>…] — or <witness-repo> --rest"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(git log:*), Bash(git rev-parse:*), Bash(rg:*), Bash(find:*), Read, Edit, Write, Glob, Grep
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
- **Never commit.** Committing is `commit` / `commit-and-pr`'s job, and leaving the work staged lets
  a human read the whole batch as one diff before it is sealed.

## Step 3 — Report, per target

One line each, and keep the states distinct:

| State | Meaning |
|---|---|
| ✅ | applied, matches the pattern |
| ⚠️ | applied with a deviation — say which, in the same line |
| ⏭️ | already had the change, nothing to do |
| ❌ | not applied — the target diverges too much for the pattern to mean anything here |

❌ is a legitimate outcome, not a failure to hide. A target where the pattern does not fit is
information the person needs; forcing it there is how a batch quietly corrupts a repo.

End with the count and the exact command to review everything at once:

```
git -C <target> diff        # per target, nothing committed
```
