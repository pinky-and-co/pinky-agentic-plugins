# pinky-agentic-plugins

<!-- pinky:readme-tagline:start -->

Public Claude Code plugin marketplace — generic, reusable workspace mechanics (git workflow, policy injection, permission guards).

<!-- pinky:readme-tagline:end -->

<!-- pinky:readme-badges:start -->

<div align="center">

![CI](https://github.com/pinky-and-co/pinky-agentic-plugins/actions/workflows/ci.yml/badge.svg)
![PyPI](https://img.shields.io/pypi/v/pinky-agentic-plugins)
![Code style](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)
[![prek](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/j178/prek/master/docs/assets/badge-v0.json)](https://github.com/j178/prek)

**[Install](#install) • [Documentation](#documentation) • [License](#license)**

</div>
<!-- pinky:readme-badges:end -->

Public Claude Code plugin marketplace — generic, reusable workspace mechanics with zero
profile-resolved values (no product codename, no employer reference, nothing that only makes
sense inside a specific org). Installable by anyone via `claude plugin marketplace add`.

## Plugins

| Plugin | What it ships |
|--------|----------------|
| [`pc-core`](plugins/pc-core/) | Generic workspace mechanics: gitmoji git workflow (`commit`, `commit-and-pr`, `clean-gone`, `branch-workflow`), the `inject_policies` hook and its `settings.pinky.json` contract, and permission guards (`careful`, `freeze`, `guard`) |
| [`pc-snow`](plugins/pc-snow/) | Snowflake anti-training-bias knowledge (distilled, provenance-tracked) — not yet populated |

## Install

```
claude plugin marketplace add pinky-and-co/pinky-agentic-plugins
claude plugin install pc-core@pinky-agentic-plugins
```

<!-- pinky:readme-why:start -->

<!-- pinky:readme-why:end -->

<!-- pinky:readme-docs:start -->

**Structure**

<details>
<summary>Show the directory tree</summary>

```text
pinky-agentic-plugins/
├── .claude/
│   ├── settings.json
│   └── settings.pinky.json
├── .claude-plugin/
│   └── marketplace.json
├── .github/
│   └── workflows/
│       ├── ci.yml
│       ├── prek.yml
│       └── release.yml
├── plugins/
│   ├── pc-core/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── agents/
│   │   │   └── reviewer.md
│   │   ├── hooks/
│   │   │   ├── hooks.json
│   │   │   ├── inject_policies.py
│   │   │   ├── keychain.py
│   │   │   ├── load_secrets.py
│   │   │   ├── policy_lib.py
│   │   │   ├── post_edit_lint.py
│   │   │   ├── privacy_egress.py
│   │   │   ├── read_cost.py
│   │   │   ├── secret_patterns.py
│   │   │   └── style_reminder.py
│   │   ├── mcp/
│   │   │   ├── model_adversarial.py
│   │   │   ├── model_offline.py
│   │   │   ├── model_panel.py
│   │   │   ├── review_complexity.py
│   │   │   └── server.py
│   │   ├── skills/
│   │   │   ├── apply-pattern/
│   │   │   │   └── SKILL.md
│   │   │   ├── council/
│   │   │   │   ├── evals/
│   │   │   │   │   └── evals.json
│   │   │   │   ├── references/
│   │   │   │   │   ├── type-generic.md
│   │   │   │   │   ├── type-pitch.md
│   │   │   │   │   └── type-tech.md
│   │   │   │   └── SKILL.md
│   │   │   ├── document-to-md/
│   │   │   │   └── SKILL.md
│   │   │   └── review-guidelines/
│   │   │       └── SKILL.md
│   │   ├── .mcp.json
│   │   └── README.md
│   ├── pc-craft/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   └── skills/
│   │       ├── code-conventions/
│   │       │   ├── references/
│   │       │   │   ├── lib-api.md
│   │       │   │   ├── repro.md
│   │       │   │   └── web.md
│   │       │   └── SKILL.md
│   │       ├── debug-brief/
│   │       │   └── SKILL.md
│   │       ├── fastapi/
│   │       │   ├── references/
│   │       │   │   └── review.md
│   │       │   └── SKILL.md
│   │       └── improve/
│   │           └── SKILL.md
│   ├── pc-git/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── hooks/
│   │   │   ├── branch-policy/
│   │   │   │   ├── .claude/
│   │   │   │   │   └── .guard-state.json
│   │   │   │   ├── branch_guard.py
│   │   │   │   ├── branch_paths.sh
│   │   │   │   ├── bulk_commit.sh
│   │   │   │   ├── resolve_policy.py
│   │   │   │   └── test_bulk_commit.sh
│   │   │   ├── hooks.json
│   │   │   └── policy_lib.py
│   │   └── skills/
│   │       ├── branch-workflow/
│   │       │   └── SKILL.md
│   │       ├── clean-gone/
│   │       │   ├── scripts/
│   │       │   │   └── bulk_clean_gone.sh
│   │       │   └── SKILL.md
│   │       ├── commit/
│   │       │   ├── references/
│   │       │   │   └── commit-convention.md
│   │       │   └── SKILL.md
│   │       └── commit-and-pr/
│   │           ├── references/
│   │           │   ├── ci-reaction.md
│   │           │   └── commit-convention.md
│   │           └── SKILL.md
│   ├── pc-kanban/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── hooks/
│   │   │   ├── hooks.json
│   │   │   ├── secret_patterns.py
│   │   │   └── stop_handoff.py
│   │   └── skills/
│   │       ├── chantier/
│   │       │   ├── scripts/
│   │       │   │   ├── archive_card.sh
│   │       │   │   └── resolve_repos.sh
│   │       │   └── SKILL.md
│   │       ├── grill/
│   │       │   └── SKILL.md
│   │       ├── kanban/
│   │       │   ├── references/
│   │       │   │   ├── finishing-a-card.md
│   │       │   │   ├── multi-board.md
│   │       │   │   └── requirements.md
│   │       │   └── SKILL.md
│   │       ├── permission-change/
│   │       │   ├── scripts/
│   │       │   │   └── guard.py
│   │       │   └── SKILL.md
│   │       └── yt/
│   │           └── SKILL.md
│   ├── pc-privacy/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── mcp/
│   │   │   ├── anonymize.py
│   │   │   └── server.py
│   │   ├── .mcp.json
│   │   └── README.md
│   ├── pc-pypi/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   └── skills/
│   │       └── dev-prerelease/
│   │           ├── docs/
│   │           │   └── reference/
│   │           │       └── reconcile-model-benchmark.md
│   │           ├── evals/
│   │           │   ├── fixtures/
│   │           │   │   ├── precision_clean_table/
│   │           │   │   │   ├── src/
│   │           │   │   │   │   └── ai.py
│   │           │   │   │   ├── features_table.md
│   │           │   │   │   └── oracle.json
│   │           │   │   └── recall_broken_promises/
│   │           │   │       ├── src/
│   │           │   │       │   ├── api_integration.py
│   │           │   │       │   ├── compute_pool.py
│   │           │   │       │   ├── database.py
│   │           │   │       │   └── grant.py
│   │           │   │       ├── features_table.md
│   │           │   │       └── oracle.json
│   │           │   └── reconcile_oracle.py
│   │           ├── scripts/
│   │           │   └── reconcile.py
│   │           └── SKILL.md
│   ├── pc-snow/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── skills/
│   │   │   ├── cortex/
│   │   │   │   ├── references/
│   │   │   │   │   ├── cortex-ai-functions.md
│   │   │   │   │   └── cortex-search.md
│   │   │   │   └── SKILL.md
│   │   │   ├── snowflake/
│   │   │   │   ├── references/
│   │   │   │   │   ├── artifact-repository.md
│   │   │   │   │   ├── bootstrap.md
│   │   │   │   │   ├── core-api.md
│   │   │   │   │   ├── costs.md
│   │   │   │   │   ├── gov.md
│   │   │   │   │   ├── native-app.md
│   │   │   │   │   ├── review.md
│   │   │   │   │   ├── telemetry.md
│   │   │   │   │   └── warehouses.md
│   │   │   │   └── SKILL.md
│   │   │   ├── snowpark/
│   │   │   │   ├── references/
│   │   │   │   │   └── snowpark.md
│   │   │   │   └── SKILL.md
│   │   │   ├── snowpark-to-sql/
│   │   │   │   └── SKILL.md
│   │   │   ├── sql-to-snowpark/
│   │   │   │   └── SKILL.md
│   │   │   ├── streamlit/
│   │   │   │   ├── references/
│   │   │   │   │   ├── review.md
│   │   │   │   │   └── streamlit.md
│   │   │   │   └── SKILL.md
│   │   │   └── .gitkeep
│   │   └── README.md
│   └── pc-statusline/
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── scripts/
│       │   ├── _common.py
│       │   ├── statusline.py
│       │   └── subagent_statusline.py
│       ├── README.md
│       └── settings.json
├── scripts/
│   └── hooks/
│       ├── badges.toml
│       ├── check_forbidden_keywords.py
│       ├── check_typed_api.py
│       ├── check_vendor_checksum.py
│       ├── check_weak_assertions.py
│       ├── check_zws.py
│       ├── commit-msg
│       ├── docs_identity.json
│       ├── generate-docs
│       ├── generate_ci_badges.py
│       ├── render_docs.py
│       ├── run-tests
│       ├── validate_frontmatter.py
│       └── validate_plugins_strict.py
├── tests/
│   ├── test_anonymize.py
│   ├── test_branch_guard.py
│   ├── test_guard.py
│   ├── test_hook_output_invariants.py
│   ├── test_inject_policies.py
│   ├── test_keychain.py
│   ├── test_load_secrets.py
│   ├── test_model_adversarial.py
│   ├── test_model_panel.py
│   ├── test_policy_lib.py
│   ├── test_post_edit_lint.py
│   ├── test_privacy_egress.py
│   ├── test_read_cost.py
│   ├── test_reconcile.py
│   ├── test_reconcile_oracle.py
│   ├── test_review_complexity.py
│   ├── test_secret_patterns.py
│   ├── test_statusline.py
│   ├── test_statusline_common.py
│   ├── test_stop_handoff.py
│   └── test_subagent_statusline.py
├── .darglint
├── .gitattributes
├── .gitignore
├── .gitleaks.toml
├── .pre-commit-config.yaml
├── .python-version
├── .secrets.baseline
├── AGENTS.md
├── ATTRIBUTIONS.md
├── CLAUDE.md
├── CREDITS
├── NOTICE
├── pyproject.toml
├── README.md
└── uv.lock
```

</details>

<!-- pinky:readme-docs:end -->

## License

<!-- pinky:readme-license:start -->

UNKNOWN — see [LICENSE](LICENSE).

<!-- pinky:readme-license:end -->
