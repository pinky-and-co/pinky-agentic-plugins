---
name: commit-convention
description: Reference knowledge for the gitmoji + Conventional-Commits-based commit message format shared by the `commit` and `commit-and-pr` skills — read before writing any commit message.
user-invocable: false
---

# commit-convention — Commit convention

Reference knowledge base for the gitmoji commit convention used across all repos.

______________________________________________________________________

## 1. Generation process

- [ ] Step 1: Analyse the diff (staged files, nature of changes)
- [ ] Step 2: Identify the primary change — if multiple logical groups, split into one commit per group
- [ ] Step 3: Choose **emoji + type** from the table below
- [ ] Step 4: Run commit(s)
  ```bash
  git commit -m "<emoji> <type>[!]: <description>"
  ```
- [ ] Step 5: If the commit is blocked by a hook — diagnose the error and propose a fix before retrying.

______________________________________________________________________

## 2. Format

```
<emoji> <type>[!]: <description> [<issue-ref>]
```

- **emoji + type**: see table below — one emoji determines one type
- **description**: required, min 3 chars, English.
- **issue-ref**: optional, end of message — `Closes: #42` or `Refs: #42`
- **`!`**: optional, right after `type`, before the colon — marks a breaking change (see below)

### Emoji ↔ Type table

> Source: [gitmoji.dev](https://gitmoji.dev) by [carloscuesta](https://github.com/carloscuesta/gitmoji/tree/master) (MIT) — maps each emoji to a semantic type.

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

## Breaking changes

Per the Conventional Commits specification, a breaking change is a **modifier on top of any type**, not a type of its own.

Signal it either way (or both):

- **`!`** right after `type`, before the `:` — e.g. `♻️ refactor!: rename upload_packages signature`
- **`BREAKING CHANGE: <description>`** as the last block of the message (blank line before it).
  One form only: both words uppercase, a single space between them. That exact casing is what
  downstream tooling (e.g. semantic-release) keys off for a major bump.

  The spec (§16) also allows `BREAKING-CHANGE:` with a hyphen as a synonym; this house style does
  not. The `commit-msg` hook rejects any near-miss at the start of a line — the hyphen form, a
  miscased `Breaking change:` — rather than letting it pass as body text, so a footer that
  wouldn't trigger the bump never slips through unnoticed.

```
♻️ refactor!: rename upload_packages signature

BREAKING CHANGE: upload_packages now takes (bucket, key) instead of (path).
```

## Gotchas

- **`.md` is not automatically `docs`** — in a plugin-marketplace repo, `SKILL.md`, agent
  definitions, and hook-config `.md` files ARE the executable unit: editing one changes what an
  agent does, the same as editing a `.py` file. Judge the emoji from what the change *does*, never
  from the file extension: a behavioral fix to a `SKILL.md` is `🐛 fix`, a new skill is `✨ feat`, a
  restructure is `♻️ refactor`. Reserve `📝 docs` for genuine prose with no behavioral effect — a
  README, a guide, a comment.
- **Multiple unrelated changes in one diff** → propose one commit per logical group, never merge them
- **Emoji chosen before reading the diff fully** → read everything before picking the emoji, late changes often shift the type
- **`BREAKING CHANGE:` in the wrong case or hyphenated** → `Breaking change:` / `BREAKING-CHANGE:` are rejected by the `commit-msg` hook — always exactly `BREAKING CHANGE:` (space, both words uppercase)
- **`💾 save`** → a human-only WIP savepoint (`💾 save: … [skip ci]`), enforced by the `commit-msg` hook; agents never write it

### Valid examples

```bash
🎉 feat: initial commit
✨ feat: add zip_python_package function
🐛 fix: handle None value in format_date
📝 docs: migrate AGENT_GITMOJI from _CODE_SRC_LEGACY
♻️ refactor: extract stage upload logic
🔧 chore: add ruff pre-commit hook
⬆️ build: upgrade snowflake to 1.12.0
♻️ refactor!: rename upload_packages signature
```
