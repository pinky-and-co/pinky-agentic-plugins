# Mods — pitfalls

Measured on Claude Code 2.1.288. The API, its doc comments and examples live in the typings the
engine writes (`<plugin>/.claude-plugin/types/claude-code/index.d.ts` once the mod has loaded): grep
them for the noun at hand before guessing.

## Permissions

- **`$.tool.check` does not see `PreToolUse` hooks nor the auto-mode classifier.** It returns the
  engine's own verdict (rules, mode, working directories). A guard hook that answers `ask` is
  invisible to it: in `auto` mode, or with the target inside a working directory, it answers
  `allow` while the guard still prompts. Never infer "a prompt is coming" from it; decide from the
  path, and skip only a `deny` or a verdict that carries a settings `rule`.
- **A mod's `tool.check` hook has the last word over a hook's `ask`.** Answering `allow` for a call
  the person approved in the mod removes the second (native) prompt. Bind that `allow` to the exact
  input shown (path + content fingerprint, `tool_use_id`), never to the tool alone.
- **Waiting for a person inside `tool.call`**: the hook has a 10 s budget, but time spent inside
  `$` calls does not count. Poll with `$.process.run(['sleep', '0.25'])` and `$.clock.now()`, not a
  bare promise; bound the wait and fall back to `next(e)` (the native prompt) when nobody answers.
- **Hold only where the prompt can be drawn**: `$.session.surfaces()` is empty under `claude -p`,
  and `AbovePrompt` (like the other prompt-side components) is raised on `terminal` and `desktop`
  only. Check that one of those two is in the list, not that it is non-empty: a VS Code-only session
  (`['vscode']`) would otherwise hold a call nobody sees until the bound expires.

## Session

- `$.session.cwd()` follows a shell `cd`; `$.session.root()` moves only with `/cd`, a host
  directory change or a worktree move. Claude Code ties per-project state (memory directory) to the
  root: derive anything project-scoped from `root()`.
- `$.plugin.root` is the plugin's own folder (the one holding `plugin.json`), synchronously.
- Module variables reset on every reload; values in `$.state` survive it.

## Drawing

- **Prefer the band (`AbovePrompt`) for anything raised unasked.** A `Pane` opened without a person's
  action seats only from 144 terminal columns, and may sit in a docked layout the person does not
  watch. A framed band (`Box borderStyle="round" borderColor=…`) reads as one prompt.
- **`Button` has no color of its own**: only `hover` (a `TextHoverProps`) and `variant`. `hover`
  needs a keyed `Box` around the Button or a `scope`, else the whole tree is refused
  ("hover has no Box with a key around it") and the engine draws its own.
- **`plain` Buttons read as text** (`1: label`). The default Button draws `[ label ]`; put an icon in
  the label and give the color through `hover`.
- **Hotkeys**: a letter acts only once the site holds the focus (ctrl+x tab, a click); a bare digit
  in an empty prompt presses a band Button. Digits that mean nothing to a person need not be drawn:
  set `hotkey` on a non-plain Button.
- A `ui.render` hook returning `next(e)` with nothing beneath needs something to answer it in tests
  (see below).

## Tests (`claude plugin test`)

- The `engine(on, …)` stand-in must answer every noun the mod calls: `process.run`, `env.get`,
  `session.surfaces`, `session.root`, `clock.now`, `ui.render`.
- `ui.render` beneath the mod must return a tree (`<Text>{''}</Text>`), not `null`: `null` is
  refused ("not a tree element") and the mount fails with "no implementation for ui.render".
- Mount a band with `{ component: 'AbovePrompt', props: { hasSurvey: false, isWorking: true, maxRows, bodyColumns } }`
  and retry the mount until the held state shows: the hold starts asynchronously.
- A test proves the hooks and the tree, never the paint: look at the real terminal once.

## Layout inside a plugin

- `hooks/<mod>/register.tsx`, declared in `hooks/hooks.json` under `"modules"`; the state contract
  in `types/index.d.ts`, named by `"types"` in `plugin.json`; tests in `tests/*.test.tsx`;
  `tsconfig.json` at the plugin root extending `./.claude-plugin/types/tsconfig.json`. The engine's
  `.claude-plugin/types/` is generated: keep it out of git.
- Checks: `claude plugin validate <plugin>`, `claude plugin test <plugin>`,
  `npx -p typescript tsc -p <plugin>`. Python gates do not cover TypeScript.
- **Reuse the plugin's Python instead of porting it**: `$.process.run(['python3',
  `${$.plugin.root}/hooks/<mod>/<script>.py`, …])` and parse its JSON stdout field by field. One
  source of truth for rules the Python hooks already apply; a failed script degrades the mod's
  offer, never blocks the call.
