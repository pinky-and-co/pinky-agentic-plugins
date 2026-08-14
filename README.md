# pinky-agentic-plugins

<!-- pinky:readme-tagline:start -->

<!-- pinky:readme-tagline:end -->

<!-- pinky:readme-badges:start -->

<!-- pinky:readme-badges:end -->

Public Claude Code plugin marketplace — generic, reusable workspace mechanics with zero
profile-resolved values (no product codename, no employer reference, nothing that only makes
sense inside a specific org). Installable by anyone via `claude plugin marketplace add`.

## Plugins

| Plugin | What it ships |
|--------|----------------|
| [`pc-commit`](plugins/pc-commit/) | Gitmoji-convention git workflow: `commit`, `commit-and-pr` (GitHub/GitLab), `clean-gone`, `branch-workflow` — plus the `inject_policies` hook and its `policies.json` contract |
| [`pc-snow`](plugins/pc-snow/) | Snowflake anti-training-bias knowledge (distilled, provenance-tracked) — not yet populated |

## Install

```
claude plugin marketplace add pinky-and-co/pinky-agentic-plugins
claude plugin install pc-commit@pinky-agentic-plugins
```

<!-- pinky:readme-why:start -->

<!-- pinky:readme-why:end -->

<!-- pinky:readme-docs:start -->

<!-- pinky:readme-docs:end -->

## License

<!-- pinky:readme-license:start -->

<!-- pinky:readme-license:end -->
