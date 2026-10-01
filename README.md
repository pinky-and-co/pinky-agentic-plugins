# pinky-agentic-plugins

<!-- pinky:readme-tagline:start -->

Public Claude Code plugin marketplace — generic, reusable workspace mechanics: git workflow, kanban, session-hygiene hooks, engineering conventions.

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

Every plugin carries zero profile-resolved values — no product codename, no employer reference,
nothing that only makes sense inside one org. Values that differ per repo (chat language, default
branch, branch strategy) are read from that repo's own `.claude/settings.pinky.json`.

## Plugins

| Plugin | What it ships |
|--------|----------------|
| [`pc-core`](plugins/pc-core/) | Session hygiene hooks (per-repo language/branch policies, costly-read and secret-read guards, post-edit lint, keychain secrets), the `reviewer` agent, skills `apply-pattern`, `council`, `document-to-md`, `review-guidelines`, and the `offline`/`adversarial`/`panel` model MCP tools |
| [`pc-git`](plugins/pc-git/) | Gitmoji commit workflow (`commit`, `commit-and-pr`, `commit-convention`), `branch-workflow`, `clean-gone` (also run at session start), and hooks guarding permanent branches and overlapping PRs |
| [`pc-kanban`](plugins/pc-kanban/) | File-based kanban mechanics — chantiers and cards (`kanban`, `chantier`, `grill`, `init`, `spawn-workers`, `yt`), the `card-worker` agent, permission guards (`careful`/`freeze`/`deny`/`guard`), card-discipline and handoff hooks |
| [`pc-autodiscovery`](plugins/pc-autodiscovery/) | Always-on activation of the project-scoped plugins matching where the session sits, and a gate on the next tool call while a user-only slash command is pending |
| [`pc-craft`](plugins/pc-craft/) | Engineering conventions: `code-conventions`, `debug-brief`, `fastapi`, `improve` |
| [`pc-delegate`](plugins/pc-delegate/) | `export`/`reintegrate`: hand kanban cards and code to an agent working outside Claude Code, then bring its results back safely |
| [`pc-privacy`](plugins/pc-privacy/) | Deterministic PII pseudonymization (Presidio + French NLP) as MCP tools, plus an optional local decision-model gate on Read |
| [`pc-pypi`](plugins/pc-pypi/) | `dev-prerelease`: cut a dev pre-release of a PyPI library, stopping before the publishing tag |
| [`pc-rtk`](plugins/pc-rtk/) | RTK output filtering: a hook rewrites Bash commands to their filtered `rtk` form before they run, and the `gain` skill shows the token savings |
| [`pc-snow`](plugins/pc-snow/) | Snowflake knowledge against training bias: `snowflake`, `snowpark`, `streamlit`, `cortex`, `snowpark-to-sql`, `sql-to-snowpark` |
| [`pc-statusline`](plugins/pc-statusline/) | Multi-line status line — model, effort, context, cache hit, cost, rate limits — plus a per-subagent row |

## Install

Prerequisites: Python 3.12+; [`uv`](https://docs.astral.sh/uv/) for the plugins shipping an MCP
server (`pc-core`, `pc-delegate`, `pc-privacy`), which resolves their dependencies with no
`pip install` step.

```bash
claude plugin marketplace add pinky-and-co/pinky-agentic-plugins
claude plugin install pc-git@pinky-agentic-plugins      # any plugin from the table
```

Inside a pinky workspace, `pinky claude-plugin install <plugin>` also asks which folders enable it
and distributes the plugin's rules. `pc-statusline` needs it: a status line has no plugin-manifest
slot, so `pinky` wires it into `~/.claude/settings.json`.

Developing a plugin from a local clone: see *Where an installed plugin runs from* in
[AGENTS.md](AGENTS.md) — what reloads on its own, and what needs `/reload-plugins`.

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
│   │   ├── hooks/
│   │   │   ├── agents-md/
│   │   │   │   ├── agents_md_hook.py
│   │   │   │   ├── agents_md_lib.py
│   │   │   │   └── agents_md_repo.py
│   │   │   ├── clear_pending_queue.py
│   │   │   ├── hooks.json
│   │   │   ├── pending_gate.py
│   │   │   ├── pending_user_action.py
│   │   │   ├── plugin_scope_lib.py
│   │   │   ├── settings_commit_nudge.py
│   │   │   └── sync_plugin_scope.py
│   │   └── skills/
│   │       └── agents-md/
│   │           └── SKILL.md
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
│   │       │   │   ├── cli.md
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
│   ├── pc-customize/
│   │   ├── output-styles/
│   │   │   └── skim-and-push.md
│   │   ├── themes/
│   │   │   └── lisible.json
│   │   └── README.md
│   ├── pc-decision/
│   │   ├── mcp/
│   │   │   ├── decision_client.py
│   │   │   └── pc_decision_server.py
│   │   └── README.md
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
│   │   │   ├── pc_git_direct_command_runner.py
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
│   │   ├── agents/
│   │   │   └── card-worker.md
│   │   ├── hooks/
│   │   │   ├── agent-log-migration/
│   │   │   │   └── pc_kanban_migrate_agent_log.py
│   │   │   ├── card-discipline/
│   │   │   │   └── card_discipline.py
│   │   │   ├── agent_log_writer.py
│   │   │   ├── hooks.json
│   │   │   ├── orchestrator_freeze.py
│   │   │   ├── pc_kanban_direct_command_runner.py
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
│   │   │   ├── init/
│   │   │   │   ├── scripts/
│   │   │   │   │   └── pc_kanban_init.py
│   │   │   │   └── SKILL.md
│   │   │   ├── kanban/
│   │   │   │   ├── references/
│   │   │   │   │   ├── finishing-a-card.md
│   │   │   │   │   ├── multi-board.md
│   │   │   │   │   ├── requirements.md
│   │   │   │   │   └── story-template.md
│   │   │   │   ├── scripts/
│   │   │   │   │   ├── next_card_recommendation.py
│   │   │   │   │   ├── open_cards_brief.py
│   │   │   │   │   └── pi_review.py
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
│   │   ├── evals/
│   │   │   ├── decision_compare.py
│   │   │   ├── gliner_compare.py
│   │   │   ├── tev_compare.py
│   │   │   └── tev_systemone.py
│   │   ├── hooks/
│   │   │   ├── decision_gate.py
│   │   │   ├── hooks.json
│   │   │   └── pii_guard.py
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
│   ├── pc-rtk/
│   │   ├── hooks/
│   │   │   ├── hooks.json
│   │   │   └── pc_rtk_direct_command_runner.py
│   │   ├── skills/
│   │   │   └── gain/
│   │   │       ├── scripts/
│   │   │       │   └── gain.sh
│   │   │       └── SKILL.md
│   │   └── README.md
│   ├── pc-snow/
│   │   ├── agents/
│   │   │   └── snowflake-guide.md
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
│       ├── hooks/
│       │   ├── hooks.json
│       │   └── quota_refresh.py
│       ├── scripts/
│       │   ├── _common.py
│       │   ├── _quota.py
│       │   ├── statusline.py
│       │   └── subagent_statusline.py
│       ├── README.md
│       └── settings.json
├── scripts/
│   └── hooks/
├── tests/
│   ├── conftest.py
│   ├── smoke_model_pool.py
│   ├── test_activate_plugins_for_repos.py
│   ├── test_agent_log_writer.py
│   ├── test_agents_md_hook.py
│   ├── test_anonymize.py
│   ├── test_apply_anonymization.py
│   ├── test_apply_reintegrate.py
│   ├── test_branch_guard.py
│   ├── test_build_export.py
│   ├── test_bulk_commit_sh.py
│   ├── test_card_discipline.py
│   ├── test_clean_gone_hook.py
│   ├── test_clear_pending_queue.py
│   ├── test_decision_client.py
│   ├── test_detect_findings.py
│   ├── test_detect_format.py
│   ├── test_direct_command_hooks.py
│   ├── test_drop_card_sh.py
│   ├── test_guard.py
│   ├── test_hook_output_invariants.py
│   ├── test_inject_policies.py
│   ├── test_keychain.py
│   ├── test_load_secrets.py
│   ├── test_model_adversarial.py
│   ├── test_model_adversarial_cli.py
│   ├── test_model_offline.py
│   ├── test_model_panel.py
│   ├── test_next_card_recommendation.py
│   ├── test_open_cards_brief.py
│   ├── test_orchestrator_freeze.py
│   ├── test_pc_core_server_offline.py
│   ├── test_pc_kanban_init.py
│   ├── test_pc_kanban_migrate_agent_log.py
│   ├── test_pc_kanban_spawn_workers.py
│   ├── test_pc_rtk.py
│   ├── test_pending_gate.py
│   ├── test_pending_user_action_channel.py
│   ├── test_pi_review.py
│   ├── test_pii_guard.py
│   ├── test_pii_guard_decision.py
│   ├── test_policy_lib.py
│   ├── test_post_edit_lint.py
│   ├── test_pr_overlap_guard.py
│   ├── test_privacy_egress.py
│   ├── test_quota.py
│   ├── test_quota_hook.py
│   ├── test_read_cost.py
│   ├── test_reconcile.py
│   ├── test_reconcile_oracle.py
│   ├── test_repo_path_flag.py
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
