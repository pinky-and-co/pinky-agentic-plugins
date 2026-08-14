---
name: branch-workflow
description: Branch naming conventions and the 6 branching strategies (Feature Branching, Git Flow, GitHub Flow, GitLab Flow, Release Branching, Trunk-Based Development) — what each implies for where a feature branch bases from and merges to. Use when creating or naming a branch, choosing a PR base branch, or explaining/comparing branching strategies.
user-invocable: false
---

# Branch workflow

A repo's actual strategy is declared in its own `.claude/policies.json` (`branch_workflow` key) —
this skill is the generic reference, not the source of truth for any specific repo. The caller
(`commit-and-pr`) already resolves that value deterministically before invoking this skill; use
this table only to interpret what the resolved strategy name means, never to re-derive it from
branch names or from CLAUDE.md prose.

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

## Choosing a base branch for a PR — decision order

1. Use the strategy name the caller already resolved from `.claude/policies.json`
   (`resolve_policy.py branch_workflow`) — that's authoritative; use the "PR base" column above
   for the named strategy, don't re-derive it from branch names or CLAUDE.md.
2. Nothing resolved (empty value) → do not guess. Ask, or default to proposing the PR against the
   branch the feature branch was created from (`git merge-base` if unclear), and flag the missing
   `.claude/policies.json` declaration so it can be added.
