# commit-convention.md — Commit convention

Reference knowledge base for the gitmoji commit convention used across all repos.

______________________________________________________________________

## 1. Generation process

- [ ] Step 1: Analyse the diff (staged files, nature of changes)
- [ ] Step 2: Identify the primary change — if multiple logical groups, split into one commit per group
- [ ] Step 3: Choose **emoji + type** from the table below
- [ ] Step 4: Propose the full command(s):
  ```bash
  git commit -m "<emoji> <type>: <description>"
  ```
- [ ] Step 5: **Wait for confirmation before running anything.**
- [ ] Step 6: Once confirmed, run commit(s) in the proposed order.
- [ ] Step 7: If the commit is blocked by a hook — diagnose the error and propose a fix before retrying.

______________________________________________________________________

## 2. Format

```
<emoji> <type>: <description> [<issue-ref>]
```

- **emoji + type**: see table below — one emoji determines one type
- **description**: required, min 3 chars, English.
- **issue-ref**: optional, end of message — `Closes: #42` or `Refs: #42`

### Emoji ↔ Type table

> Source: [gitmoji.dev](https://gitmoji.dev) by [carloscuesta](https://github.com/carloscuesta/gitmoji/tree/master) — maps each emoji to a semantic type and enforces the `<emoji> <type>: <description>` format.

| Emoji | Type | Usage |
|-------|------|-------|
| 🎉 | feat | Initial commit only |
| ✨ | feat | Introduce new features |
| 📈 | feat | Add or update analytics |
| 🌐 | feat | Internationalization / i18n |
| 🚩 | feat | Add or update feature flags |
| 🛂 | feat | Authorization / roles / permissions |
| 👔 | feat | Add or update business logic |
| ♿️ | feat | Improve accessibility |
| 🦺 | feat | Add or update validation code |
| 🚸 | feat | Improve user experience / usability |
| 📱 | feat | Work on responsive design |
| 🥚 | feat | Add or update an easter egg |
| 🔍️ | feat | Improve SEO |
| 🧵 | feat | Add or update code related to multithreading / concurrency |
| 🌍 | feat | Add or update localization |
| 🐛 | fix | Fix a bug |
| 🥅 | fix | Catch errors |
| 🩹 | fix | Simple fix for non-critical issue |
| 🚑️ | fix | Critical hotfix |
| 🔒️ | fix | Fix security issues |
| 👽️ | fix | Update code due to external API change |
| 🚨 | fix | Fix compiler / linter warnings |
| 🍎 | fix | Fix something on macOS |
| 🐧 | fix | Fix something on Linux |
| 🏁 | fix | Fix something on Windows |
| 🤖 | fix | Fix something on Android |
| 🍏 | fix | Fix something on iOS |
| 📝 | docs | Add or update documentation |
| 💡 | docs | Add comments in source code |
| 💬 | docs | Add or update text / strings |
| 📄 | docs | Add or update license |
| 👥 | docs | Add or update contributor(s) |
| 🎨 | style | Improve code structure / format |
| ✏️ | style | Fix typos |
| 💄 | style | Update UI / styles |
| 💫 | style | Add or update animations and transitions |
| ♻️ | refactor | Refactor code |
| 🔥 | refactor | Remove code or files |
| 🗑️ | refactor | Deprecate code |
| ⚰️ | refactor | Remove dead code |
| 🗃️ | refactor | Database related changes |
| 🏗️ | refactor | Architectural changes |
| 💩 | refactor | Write bad code that needs to be improved |
| 🍻 | refactor | Write code drunkenly (hackish/temporary solution) |
| 🚚 | move | Move or rename resources |
| ⚡️ | perf | Improve performance |
| ✅ | test | Add or update tests |
| 🧪 | test | Add a failing test |
| 🤡 | test | Mock things |
| 📸 | test | Add or update snapshots |
| 🩺 | test | Add or update healthcheck |
| ⏪️ | revert | Revert changes |
| 📦️ | build | Add or update compiled packages |
| ⬆️ | build | Upgrade dependencies |
| ⬇️ | build | Downgrade dependencies |
| ➕ | build | Add a dependency |
| ➖ | build | Remove a dependency |
| 📌 | build | Pin dependencies to specific version |
| 🔧 | chore | Add or update config files |
| 🔨 | chore | Add or update dev scripts |
| 👷 | chore | Add or update CI system |
| 💚 | chore | Fix CI build |
| 🔊 | chore | Add or update logs |
| 🔇 | chore | Remove logs |
| 🔐 | chore | Add or update secrets |
| 🚧 | chore | Work in progress |
| 🔀 | chore | Merge branches |
| 🍱 | chore | Add or update assets |
| 🏷️ | chore | Add or update types |
| 🌱 | chore | Add or update seeds |
| 🧱 | chore | Infrastructure changes |
| 🙈 | chore | Add or update .gitignore file |
| ⚗️ | chore | Perform experiments |
| 💸 | chore | Add sponsorships or financial-related changes |
| 🚀 | release | Deploy / release |
| 🔖 | release | Release / version tags |
| 💥 | breaking | Introduce breaking changes |
| 💾 | save | WIP savepoint — always with [skip ci] |

### Special `save` rule

Reserved for in-progress savepoints. Describes **what is saved**, not what was done.
`[skip ci]` is **mandatory**.

```
💾 save: <what is being saved> [skip ci]
```

## Gotchas

- **Multiple unrelated changes in one diff** → propose one commit per logical group, never merge them
- **`save` without `[skip ci]`** → mandatory, always — missing it triggers CI for a WIP savepoint
- **Emoji chosen before reading the diff fully** → read everything before picking the emoji, late changes often shift the type

### Valid examples

```bash
🎉 feat: initial commit
✨ feat: add zip_python_package function
🐛 fix: handle None value in format_date
📝 docs: migrate AGENT_GITMOJI from _CODE_SRC_LEGACY
♻️ refactor: extract stage upload logic
🔧 chore: add ruff pre-commit hook
⬆️ build: upgrade snowflake to 1.12.0
💥 breaking: rename upload_packages signature
💾 save: packages.py zip logic in progress [skip ci]
```
