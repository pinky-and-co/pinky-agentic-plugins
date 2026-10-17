# Python CLI output conventions

What a Python CLI prints, in what order, in what color, and how it prompts — the level every CLI
in the suite starts at, rather than a monochrome baseline improved one flag at a time.

---

## Help: every command explains itself

`--help` is the first output anyone reads — held to the same bar as the report. With `argparse`:

- **Subject → verb** (`mytool plugin install`), each subject's verbs a sub-parser with
  `required=True`, so a bare `mytool plugin` prints its verbs instead of doing something. The entry
  point is the exception: a bare `mytool` prints the full `--help` and exits 1 (a manual check on
  the missing command), where `required=True` would print only a usage error.
- **One summary string per sub-command, used twice** — as `help=` (its line in the parent's list)
  and as `description=` (the top of its own `--help`). One string, so the two never drift. It says
  what the command does, not how: `install, place and audit plugins across the workspace`.
- **Every option says** what it does, its default **and where that default comes from** (an env
  var, a marker file), when it becomes required (`required in a non-interactive shell`), and what
  it bypasses (`repeatable — bypasses the table`). A flag with no default stated reads as "no idea".
- **Examples on every sub-command**, in an epilog built by one helper so they stay aligned:

```python
def examples(lines: list[str]) -> str:
    """`command  # effect` pairs, the comments aligned in one column."""
    pairs = [line.split("  # ", 1) for line in lines]
    width = max(len(pair[0]) for pair in pairs)
    body = "\n".join(
        f"  {pair[0]:<{width}}  # {pair[1]}" if len(pair) == 2 else f"  {pair[0]}"
        for pair in pairs
    )
    return f"examples:\n{body}"


def add_command(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
    name: str,
    summary: str,
    example_lines: list[str],
) -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        help=summary,
        description=summary,
        epilog=examples(example_lines),
        formatter_class=argparse.RawDescriptionHelpFormatter,  # keeps the epilog's layout
    )
```

- **The top-level epilog is an index** of what `--help` cannot list by itself (companion scripts,
  where the docs live), not a repeat of the command list.
- **No internal vocabulary** — no requirement ids or ticket numbers anywhere in the code (help,
  comments, docstrings, tests): they point to a spec outside the repo, pruned once implemented, so
  the reference leads nowhere — state the behaviour instead. Team jargon ("dev cache-bust") stays
  out of help a user reads.
- **An emoji marker per sub-command** is fine as a scan aid, but it must print everywhere the text
  goes: shell completion menus show descriptions through the shell's own printability check, and
  zsh on macOS escapes emoji added after Unicode 10 (`\U0001f9e9`) — keep them below U+1F900, and
  enforce it with a test.
- **Completion** (`argcomplete`) is part of the help surface: a `completion` sub-command printing
  the registration script beats a setup step documented elsewhere.

## What needs action comes first, success collapses to one line

A report that lists every check clutters the one thing to act on. Order:

1. What is missing/wrong, in full, each with its remedy.
2. One summary line for everything that is fine — `✓ all 12 present`, not 12 more lines.
3. `--verbose` re-expands step 2 into the full per-item list, grouped by category.

```
✗ 2 prerequisites missing

  ✗ ripgrep — brew install ripgrep (required by: search-plugin)
  ✗ OPENAI_API_KEY — set $OPENAI_API_KEY, or add it to the keychain

✓ 10 present — `mytool doctor --verbose` lists them
```

Never a bare count standing in for names: "10 present" is a summary of items already named
elsewhere or available on demand, not a substitute for naming a *changed* thing. A diff-shaped
report (files added/updated/removed) always names the file, not `+2 ~1 -0`.

## Status glyphs, always paired with text

`✓` success, `✗` failure, `⚠` warning — never the glyph alone, never color alone carrying the
distinction: a `NO_COLOR` run or a colorblind reader must get the same information from the text.

```
✓ Deployment complete
✗ Config file not found
⚠ Deprecated flag --old-flag, use --new-flag instead
```

Section headers are bold, secondary/explanatory text (a detail after an em dash, a byte count) is
dimmed. Both are the same "color is decoration" rule applied to structure instead of status: a
`NO_COLOR` reader still sees the text, just not the emphasis.

## Color is a hint, decided once per run

Three independent gates, all of which must pass before any ANSI code is emitted — a `use_color()`
resolved once at startup, threaded to every render call rather than re-checked per line:

```python
import os
import sys
from collections.abc import Mapping
from typing import TextIO


def use_color(
    stream: TextIO | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    out = stream if stream is not None else sys.stdout
    env = os.environ if environ is None else environ
    return out.isatty() and not env.get("NO_COLOR") and env.get("TERM") != "dumb"
```

- **Not a terminal** (piped to a file, captured in a test, CI log) → off.
- **`NO_COLOR` set to anything non-empty** → off, per https://no-color.org/ — never a project-local
  reinvention of the same switch.
- **`TERM=dumb`** → off, even on a real tty.

A `paint(text, code, *, color: bool)` helper that no-ops when `color` is False keeps every call site
identical whether color is on or off — no `if color: ... else: ...` duplicated at each print.

### Light vs dark theme

A color legible on a dark background can be illegible on a light one (reverse-video selection,
pastel diff backgrounds) — decide the theme once, the same way as `use_color`, and pick the palette
that stays legible on it. An explicit override (an env var read before any platform probe) beats
guessing; without it, ask the OS (dark-mode setting) or a terminal-reported hint
(`COLORFGBG`), defaulting to whichever theme is legible on ambiguous output.

## Plan → confirm → report, one vocabulary

Every mutating command follows the same three-beat shape, with the same words every time so a
reader who has seen it once recognizes it everywhere:

1. **Plan** — what would change, named (files, targets, keys) — never only counted.
2. **`[y/N]`** — skipped by `--yes`; `--dry-run` prints the plan and writes nothing, ever.
3. **Report** — what happened, in the plan's own words.

```
mytool:
  add: staging, production
  remove: sandbox

Apply this plan? [y/N] y
applied.
```

Nothing to change is `nothing to do` — not a silently empty plan, not a `0 changes` line that reads
like a bug. A plan with real changes never prints `applied.` before the prompt resolves, and
`dry-run`/`declined`/`applied` are three distinct, named outcomes a caller (a test, a script) can
tell apart — never inferred from stdout alone.

## Non-interactive and interrupted runs

- **No tty** (piped, CI, a subprocess) never prompts — a prompt with nothing reading stdin hangs or
  reads garbage. Fail immediately with a message naming the flag that replaces the prompt:
  `Non-interactive shell — pass --yes to apply.` Same for a picker with nothing to pick from
  interactively: name the flag that gives the value directly (`--target`), not a generic error.
- **Ctrl-C** exits 130 (128 + SIGINT, the shell convention), one line on stderr, no traceback — catch
  `KeyboardInterrupt` once, at the entry point, then `print("cancelled.", file=sys.stderr)` and
  `sys.exit(130)`. A stack trace on the most ordinary way to cancel a CLI reads as a crash.
- **Diagnostics go to stderr** — anything that is not the requested output (a progress notice, a
  warning, "not installed, skipped") uses `file=sys.stderr`, so a caller can pipe stdout into
  another tool without also capturing narration.

## Dependency: stdlib first, a real UI library when going without means rebuilding it

Take a dependency when doing without it means re-implementing it; when the standard library does
the job without complicating the code, use the standard library. Two real shapes:

- **Stdlib is enough** — color codes, status glyphs, `isatty`/`NO_COLOR`/`TERM` gating, and a
  light/dark theme guess fit in a few dozen lines of `os`/`sys`: a dependency would buy nothing a
  handful of functions don't already give.
- **A dependency earns its place** — a CLI needing a colored diff table, a per-item spinner
  collapsing to a checkmark, and a plain-text fallback under CI needs all three behaviors kept in
  sync; without a terminal-UI library, that is the library, rewritten and now owned locally.

Before taking one, ask: how many lines to do without it? Is this a component (a table renderer, a
spinner, an interactive widget) the project would then have to maintain itself? Is the dependency
stable, with no risky transitive tree? Does the project already carry a written dependency
constraint (a policy doc, an ADR) that decides this either way? **Record the decision in an ADR**
whichever way it goes — the reasoning belongs there, not only in the diff that adds the import.

## Interactive selection — a checkbox library, not a hand-rolled prompt loop

Multi-select (which of N targets to act on) is exactly a checkbox library's job — arrow/space/enter
navigation, pre-checked entries, disabled grouping rows are what it exists for. `questionary` is one
such library; measured pitfalls worth knowing before reaching for it (version 2.1.1):

- `Choice(disabled="")` does **not** disable an entry — `disabled` is truthy-checked. Use
  `disabled=True` for a row with nothing to explain (a tree's grouping node), or a non-empty string,
  which also shows in parentheses as the reason.
- A choice's `title` accepts exactly one formatted shape: a plain **list of `(style, text)`
  tuples** — any other formatted-text object (`prompt_toolkit`'s `FormattedText`/`ANSI`) prints as
  its own repr (`● ANSI('…')`). A styled title renders verbatim and loses the library's own
  checked/pointer styling, so plain strings for simple rows; indent them yourself for a tree shape
  (`"  " * depth + text`), there is no separate tree widget. A `\n` inside a tuple's text renders
  as a real line break, so a row can carry secondary lines (a description, a warning) under it.
- prompt_toolkit's style grammar has **no `dim`** attribute — the dim look is
  `fg:ansibrightblack`; a raw SGR code has no place in a style string.
- The default `("selected", "")` style leaves the underlying prompt toolkit's reverse video in
  place — a black block on a light theme. Pass a `Style` with `noreverse` on `selected` (the
  class that carries it) — setting it on `highlighted` and `pointer` too costs nothing and keeps
  the three consistent.
- `.ask()` swallows Ctrl-C into a `None` return — use `.unsafe_ask()` instead, so the
  `KeyboardInterrupt` actually reaches the CLI's own exit-130 handler.
- `initial_choice` starts the cursor on a meaningful row (e.g. the first already-checked one), and
  `instruction=` replaces the default key-hint line when the screen needs a longer legend.
- Styling a single row based on state that changed *during* the session (graying an item that was
  checked and got unchecked) is not something the public API exposes — do not patch internals to
  fake it; drop the requirement instead.
- Never call it outside a real terminal — same `isatty()` gate as everything else interactive here.

## Testing

- Color is off by default in a test run — either don't set `NO_COLOR`/isatty at all (a test runner's
  captured stdout is never a tty, so `use_color()` already resolves False) or pass `color=False`
  explicitly to the renderer under test, never rely on the ambient environment.
- At least one test per renderer asserts **equality on the whole rendered string**, not a substring:
  a substring assertion passes on a correct render and on a subtly wrong one (misplaced newline,
  extra section) alike.
- Fake only the terminal boundary (`isatty`, the environment mapping, a picker function) — the
  renderer itself runs for real. A test that replaces the renderer with a stub proves nothing about
  what a user would actually see.
