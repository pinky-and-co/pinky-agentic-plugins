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
| [`pc-core`](plugins/pc-core/) | Generic workspace mechanics: gitmoji git workflow (`commit`, `commit-and-pr`, `clean-gone`, `branch-workflow`), the `inject_policies` hook and its `policies.json` contract, and permission guards (`careful`, `freeze`, `guard`) |
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
│   └── policies.json
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
│   │   │   ├── branch-policy/
│   │   │   │   ├── branch_guard.py
│   │   │   │   ├── branch_paths.sh
│   │   │   │   ├── bulk_commit.sh
│   │   │   │   ├── resolve_policy.py
│   │   │   │   └── test_bulk_commit.sh
│   │   │   ├── hooks.json
│   │   │   ├── inject_policies.py
│   │   │   ├── policy_lib.py
│   │   │   ├── post_edit_lint.py
│   │   │   ├── privacy_egress.py
│   │   │   ├── read_cost.py
│   │   │   ├── secret_patterns.py
│   │   │   ├── stop_handoff.py
│   │   │   └── style_reminder.py
│   │   ├── mcp/
│   │   │   ├── model_adversarial.py
│   │   │   ├── model_offline.py
│   │   │   └── server.py
│   │   ├── skills/
│   │   │   ├── apply-pattern/
│   │   │   │   └── SKILL.md
│   │   │   ├── branch-workflow/
│   │   │   │   └── SKILL.md
│   │   │   ├── chantier/
│   │   │   │   ├── scripts/
│   │   │   │   │   ├── archive_card.sh
│   │   │   │   │   └── resolve_repos.sh
│   │   │   │   └── SKILL.md
│   │   │   ├── clean-gone/
│   │   │   │   └── SKILL.md
│   │   │   ├── commit/
│   │   │   │   ├── references/
│   │   │   │   │   └── commit-convention.md
│   │   │   │   └── SKILL.md
│   │   │   ├── commit-and-pr/
│   │   │   │   ├── references/
│   │   │   │   │   └── commit-convention.md
│   │   │   │   └── SKILL.md
│   │   │   ├── document-to-md/
│   │   │   │   └── SKILL.md
│   │   │   ├── kanban/
│   │   │   │   └── SKILL.md
│   │   │   ├── permission-change/
│   │   │   │   ├── scripts/
│   │   │   │   │   └── guard.py
│   │   │   │   └── SKILL.md
│   │   │   └── review-guidelines/
│   │   │       └── SKILL.md
│   │   ├── .mcp.json
│   │   └── README.md
│   ├── pc-craft/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   └── skills/
│   │       └── code-conventions/
│   │           ├── references/
│   │           │   ├── lib-api.md
│   │           │   ├── repro.md
│   │           │   └── web.md
│   │           └── SKILL.md
│   └── pc-snow/
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── skills/
│       │   ├── cortex/
│       │   │   ├── references/
│       │   │   │   ├── cortex-ai-functions.md
│       │   │   │   └── cortex-search.md
│       │   │   └── SKILL.md
│       │   ├── snowflake/
│       │   │   ├── references/
│       │   │   │   ├── artifact-repository.md
│       │   │   │   ├── bootstrap.md
│       │   │   │   ├── core-api.md
│       │   │   │   ├── costs.md
│       │   │   │   ├── gov.md
│       │   │   │   ├── native-app.md
│       │   │   │   ├── telemetry.md
│       │   │   │   └── warehouses.md
│       │   │   └── SKILL.md
│       │   ├── snowpark/
│       │   │   ├── references/
│       │   │   │   └── snowpark.md
│       │   │   └── SKILL.md
│       │   ├── streamlit/
│       │   │   ├── references/
│       │   │   │   └── streamlit.md
│       │   │   └── SKILL.md
│       │   └── .gitkeep
│       └── README.md
├── scripts/
│   └── hooks/
│       ├── badges.toml
│       ├── check_forbidden_keywords.py
│       ├── check_typed_api.py
│       ├── check_weak_assertions.py
│       ├── check_zws.py
│       ├── commit-msg
│       ├── docs_identity.json
│       ├── generate-docs
│       ├── generate_ci_badges.py
│       ├── render_docs.py
│       ├── run-tests
│       └── validate_frontmatter.py
├── tests/
│   ├── test_branch_guard.py
│   ├── test_inject_policies.py
│   ├── test_model_adversarial.py
│   ├── test_policy_lib.py
│   ├── test_post_edit_lint.py
│   ├── test_privacy_egress.py
│   ├── test_read_cost.py
│   ├── test_secret_patterns.py
│   └── test_stop_handoff.py
├── .darglint
├── .gitattributes
├── .gitignore
├── .gitleaks.toml
├── .pre-commit-config.yaml
├── .python-version
├── .secrets.baseline
├── AGENTS.md
├── CLAUDE.md
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
