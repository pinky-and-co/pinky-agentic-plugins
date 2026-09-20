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
├── plugins/
│   ├── pc-autodiscovery/
│   │   └── hooks/
│   │       ├── clear_pending_queue.py
│   │       ├── hooks.json
│   │       ├── pending_gate.py
│   │       ├── pending_user_action.py
│   │       ├── plugin_scope_lib.py
│   │       ├── settings_commit_nudge.py
│   │       └── sync_plugin_scope.py
│   ├── pc-core/
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
│   │   │   ├── structure_guard.py
│   │   │   └── style_reminder.py
│   │   ├── mcp/
│   │   │   ├── model_adversarial.py
│   │   │   ├── model_offline.py
│   │   │   ├── model_panel.py
│   │   │   ├── pc_core_server.py
│   │   │   └── review_complexity.py
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
│   │   └── README.md
│   ├── pc-craft/
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
│   ├── pc-delegate/
│   │   ├── mcp/
│   │   │   ├── pc_delegate_server.py
│   │   │   └── subtree.py
│   │   └── skills/
│   │       ├── export/
│   │       │   ├── references/
│   │       │   │   └── GUIDE.md
│   │       │   ├── scripts/
│   │       │   │   ├── build_export.py
│   │       │   │   └── secrets_filter.py
│   │       │   └── SKILL.md
│   │       └── reintegrate/
│   │           ├── scripts/
│   │           │   └── apply_reintegrate.py
│   │           └── SKILL.md
│   ├── pc-git/
│   │   ├── agents/
│   │   │   └── git-workflow.md
│   │   ├── evals/
│   │   │   ├── 01-gh-guarded-push-prfails-honest/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── correct-flow.md
│   │   │   │   │   ├── no-force-push.md
│   │   │   │   │   └── skill-fired.md
│   │   │   │   └── prompt.md
│   │   │   ├── 02-gh-push-conflict-no-force/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── conflict-handled-cleanly.md
│   │   │   │   │   ├── no-force-push.md
│   │   │   │   │   └── skill-fired.md
│   │   │   │   └── prompt.md
│   │   │   ├── 03-gh-exempt-noargs-ok/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── exempt-direct-push.md
│   │   │   │   │   ├── no-force-push.md
│   │   │   │   │   └── skill-fired.md
│   │   │   │   └── prompt.md
│   │   │   ├── 04-gh-gitflow-base-develop/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── base-branch-develop.md
│   │   │   │   │   ├── no-force-push.md
│   │   │   │   │   └── skill-fired.md
│   │   │   │   └── prompt.md
│   │   │   ├── 05-orchestrator-noarg-hardfail/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── hardfail-message.md
│   │   │   │   │   └── no-bulk-script-call.md
│   │   │   │   └── prompt.md
│   │   │   ├── 06-neg-unrelated-task/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── answers-question.md
│   │   │   │   │   ├── no-git-mutation.md
│   │   │   │   │   └── skill-not-fired.md
│   │   │   │   └── prompt.md
│   │   │   ├── 07-neg-status-only/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── no-git-mutation.md
│   │   │   │   │   ├── reports-status-only.md
│   │   │   │   │   └── skill-not-fired.md
│   │   │   │   └── prompt.md
│   │   │   ├── 08-precommit-lint-nonautofix-agent-fixes/
│   │   │   │   ├── graders/
│   │   │   │   │   ├── diagnosis-before-fix.md
│   │   │   │   │   ├── fixed-bare-except.md
│   │   │   │   │   └── no-force-push.md
│   │   │   │   └── prompt.md
│   │   │   └── 09-precommit-test-blocks/
│   │   │       ├── graders/
│   │   │       │   ├── blocks-no-cheat.md
│   │   │       │   └── nothing-pushed.md
│   │   │       └── prompt.md
│   │   ├── hooks/
│   │   │   ├── branch-policy/
│   │   │   │   ├── branch_guard.py
│   │   │   │   ├── branch_paths.sh
│   │   │   │   ├── bulk_commit.sh
│   │   │   │   ├── resolve_policy.py
│   │   │   │   └── test_bulk_commit.sh
│   │   │   ├── clean-gone/
│   │   │   │   └── clean_gone_hook.py
│   │   │   ├── pr-overlap/
│   │   │   │   └── pr_overlap_guard.py
│   │   │   ├── hooks.json
│   │   │   └── pc_git_policy_lib.py
│   │   ├── skills/
│   │   │   ├── branch-workflow/
│   │   │   │   └── SKILL.md
│   │   │   ├── clean-gone/
│   │   │   │   ├── scripts/
│   │   │   │   │   ├── bulk_clean_gone.sh
│   │   │   │   │   └── test_bulk_clean_gone.sh
│   │   │   │   └── SKILL.md
│   │   │   ├── commit/
│   │   │   │   └── SKILL.md
│   │   │   ├── commit-and-pr/
│   │   │   │   ├── references/
│   │   │   │   │   ├── ci-reaction.md
│   │   │   │   │   └── conflict-reaction.md
│   │   │   │   └── SKILL.md
│   │   │   └── commit-convention/
│   │   │       └── SKILL.md
│   │   └── README.md
│   ├── pc-kanban/
│   │   ├── hooks/
│   │   │   ├── hooks.json
│   │   │   ├── orchestrator_freeze.py
│   │   │   ├── pc_kanban_secret_patterns.py
│   │   │   └── stop_handoff.py
│   │   ├── skills/
│   │   │   ├── chantier/
│   │   │   │   ├── scripts/
│   │   │   │   │   ├── activate_plugins_for_repos.py
│   │   │   │   │   ├── archive_card.sh
│   │   │   │   │   ├── drop_card.sh
│   │   │   │   │   ├── pc_kanban_pending_user_action.py
│   │   │   │   │   ├── pc_kanban_plugin_scope_lib.py
│   │   │   │   │   └── resolve_repos.sh
│   │   │   │   └── SKILL.md
│   │   │   ├── grill/
│   │   │   │   └── SKILL.md
│   │   │   ├── kanban/
│   │   │   │   ├── references/
│   │   │   │   │   ├── finishing-a-card.md
│   │   │   │   │   ├── multi-board.md
│   │   │   │   │   ├── requirements.md
│   │   │   │   │   └── story-template.md
│   │   │   │   ├── scripts/
│   │   │   │   │   └── next_card_recommendation.py
│   │   │   │   └── SKILL.md
│   │   │   ├── permission-change/
│   │   │   │   ├── scripts/
│   │   │   │   │   └── guard.py
│   │   │   │   └── SKILL.md
│   │   │   ├── spawn-workers/
│   │   │   │   ├── scripts/
│   │   │   │   │   ├── pc_kanban_spawn_plugin_scope_lib.py
│   │   │   │   │   └── pc_kanban_spawn_workers.py
│   │   │   │   └── SKILL.md
│   │   │   └── yt/
│   │   │       └── SKILL.md
│   │   └── README.md
│   ├── pc-privacy/
│   │   ├── mcp/
│   │   │   ├── anonymize.py
│   │   │   └── pc_privacy_server.py
│   │   └── README.md
│   ├── pc-pypi/
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
│   │   │   └── streamlit/
│   │   │       ├── references/
│   │   │       │   ├── review.md
│   │   │       │   └── streamlit.md
│   │   │       └── SKILL.md
│   │   └── README.md
│   └── pc-statusline/
│       ├── scripts/
│       │   ├── _common.py
│       │   ├── statusline.py
│       │   └── subagent_statusline.py
│       ├── README.md
│       └── settings.json
├── scripts/
│   └── hooks/
├── tests/
│   ├── test_activate_plugins_for_repos.py
│   ├── test_anonymize.py
│   ├── test_apply_reintegrate.py
│   ├── test_branch_guard.py
│   ├── test_build_export.py
│   ├── test_bulk_commit_sh.py
│   ├── test_clean_gone_hook.py
│   ├── test_clear_pending_queue.py
│   ├── test_drop_card_sh.py
│   ├── test_guard.py
│   ├── test_hook_output_invariants.py
│   ├── test_inject_policies.py
│   ├── test_keychain.py
│   ├── test_load_secrets.py
│   ├── test_model_adversarial.py
│   ├── test_model_panel.py
│   ├── test_next_card_recommendation.py
│   ├── test_orchestrator_freeze.py
│   ├── test_pc_kanban_spawn_workers.py
│   ├── test_pending_gate.py
│   ├── test_policy_lib.py
│   ├── test_post_edit_lint.py
│   ├── test_pr_overlap_guard.py
│   ├── test_privacy_egress.py
│   ├── test_read_cost.py
│   ├── test_reconcile.py
│   ├── test_reconcile_oracle.py
│   ├── test_review_complexity.py
│   ├── test_secret_patterns.py
│   ├── test_settings_commit_nudge.py
│   ├── test_statusline.py
│   ├── test_statusline_common.py
│   ├── test_stop_handoff.py
│   ├── test_structure_guard.py
│   ├── test_subagent_statusline.py
│   ├── test_subtree.py
│   └── test_sync_plugin_scope.py
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
