---
name: branch-workflow
description: Branch naming conventions and the 6 branching strategies (Feature Branching, Git Flow, GitHub Flow, GitLab Flow, Release Branching, Trunk-Based Development) — what each implies for where a feature branch bases from and merges to. Use when creating or naming a branch, choosing a PR base branch, or explaining/comparing branching strategies.
user-invocable: false
---

# Branch workflow

A repo's actual strategy is declared in its own `.claude/settings.pinky.json` (`branch_workflow` key) —
this skill is the generic reference. The caller (`commit-and-pr`) already resolves that value
before invoking this skill; use the table only to interpret the resolved strategy name, never to
re-derive it from branch names or from CLAUDE.md prose.

## Naming conventions

- Lowercase, hyphen-separated: `feature/add-login`, not `Feature/AddLogin`
- Prefix by intent: `feature/`, `release/`, `hotfix/` (or `fix/` outside Git Flow)
- Keep the description short and specific — a branch name is a summary, not a changelog

## The 6 strategies

| Strategy | Permanent branches | Feature branch bases from | Feature branch merges to | Hotfix path | PR base for a `feature/*` branch |
|---|---|---|---|---|---|
| **Feature Branching** | `main` | `main` | `main` | Same as any feature | `main` |
| **Git Flow** | `main`, `develop` | `develop` | `develop` | `hotfix/*` from `main`, merged to **both** `main` and `develop` | `develop` |
| **GitHub Flow** | `main` | `main` | `main`, deployed immediately after merge | Same as any feature — ship fast, revert if broken | `main` |
| **GitLab Flow** | `main` (+ environment branches, e.g. `staging`, `production`) | `main` | `main`, then promoted through environment branches | Cherry-picked forward through environment branches | `main` |
| **Release Branching** | `main` (+ `release/*` cut per version) | `main` or `develop` depending on variant | same as base | Fixed directly on the active `release/*` branch, then back-merged | `main` (or `develop` if paired with Git Flow) |
| **Trunk-Based Development** | `main`/`trunk` only | `main` | `main`, very short-lived (hours, not days) | Directly on `main`, small enough not to need a separate path | `main` |

## Guard — is a feature branch required before committing

Any skill about to `git add`/`git commit` applies this before staging anything. Server-side
rejection of a direct push cannot be assumed — this guard stands in for it.

1. `git branch --show-current` — the current branch.
2. Is it one of the strategy's **permanent branches** (the table above, resolved strategy from
   `.claude/settings.pinky.json` as in "Choosing a base branch" below)? If not — already on a topic
   branch, nothing to do, proceed to commit.
3. On a permanent branch — check every changed file (`git status --porcelain`) against the
   non-guarded list, the single source of truth being
   `${CLAUDE_PLUGIN_ROOT}/hooks/branch-policy/branch_paths.sh <repo-root-relative-path>` (exit 0 =
   exempt). It names documentation, repo config and the lockfile — not every `*.md`, never a
   `SKILL.md` or a rule. **Every** changed file must match — one code file mixed in loses the
   exemption for the whole change.
4. All exempt → commit directly, no branch needed.
5. Not all exempt → create and switch to a feature branch first (naming conventions above), THEN
   commit — never commit on the permanent branch and branch off afterward.

## Choosing a base branch for a PR — decision order

1. Use the strategy name the caller already resolved from `.claude/settings.pinky.json`
   (`resolve_policy.py branch_workflow`) — authoritative; take the "PR base" column above for it.
2. Nothing resolved (empty value) → do not guess. Ask, or default to proposing the PR against the
   branch the feature branch was created from (`git merge-base` if unclear), and flag the missing
   `.claude/settings.pinky.json` declaration so it can be added.
