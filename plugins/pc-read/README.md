# pc-read

Cheaper reads of heavy local documents. A `PreToolUse` hook asks before a native `Read` of a
`.pdf`, `.html`, `.xlsx`, `.docx` or `.pptx`, a band above the prompt turns that question into three
buttons, and the `document-to-md` skill converts such a file to Markdown first.

`pc-core` does not carry any of this and does not depend on `pc-read`: install `pc-read` to get the
guard and the skill (`pc-read:document-to-md`).

## Overview

What makes a read "costly" is the format: the suffix list above, with no size threshold. A large
`.csv` or `.log` does not convert into prose, so size would be another function.

The gain depends on the format. A `.xlsx` refuses a native read (the conversion is then the only
path); a `.pdf` or `.html` reads natively, markup included, and the saving there is not measured.

- `hooks/read_cost.py` answers `ask`, never `deny`: a scanned or layout-dependent PDF needs a native
  read. The hook never raises, so a bug of its own cannot block a tool call.
- `skills/document-to-md/` converts a document with `markitdown` and reads the Markdown, in the
  calling session. Without `markitdown` it falls back to `Read` and says how to install it.

## Read band

For a `Read` the hook flags, a mod (`hooks/read-cost-hold/register.tsx`, written for the Claude Code
mods API, early access, measured on 2.1.288) holds the call and draws a framed band above the prompt:
`⚠︎ Costly to read as-is` and the file name, then one button per outcome, in this order for every
format (the first press wins):

- `⎘ convert to Markdown` (`1`) runs `markitdown` into a fresh private folder, then refuses the
  `Read` naming the Markdown file; the next read is of that file, whose suffix is not costly. Hidden
  when `markitdown` is not on the PATH: the band then says how to install it
  (`uv tool install 'markitdown[all]'`). A conversion that fails refuses the `Read` and says so.
- `✔︎ read as-is` (`2`) lifts the hook's `ask` for this call only: the same file read again is held
  again.
- `✘ deny` (`0`) refuses the `Read`; Claude asks what to do instead.

A file `pc-privacy` flags as personal data is not held here: the band steps aside and `pc-privacy`'s
band decides. The mod finds that plugin's guard through the folder `pc-privacy` publishes in the
shared state, and asks `pii_guard.py --verdict` before drawing its band, so a `.xlsx` or `.html`
costs one more scan per `Read`; a real PDF or Office binary is skipped by the guard's format
detection. That wait still needs a working `uv`: for a `.pdf`, `.docx` or `.pptx` too, a cold first
run or a missing `uv` removes the band and the native dialog applies.

- `pc-privacy` is absent (nothing published): the read is held as usual.
- `pc-privacy` is published but its guard fails, times out, exits non-zero or prints something else
  than JSON: the band steps aside too — nothing is held or remembered, and the decision goes to
  `pc-privacy`'s band or to the native dialog.
- Only a successful `flagged: false` answer lets this band hold the read.
- Reading the shared state itself fails: the read is held as usual, so "read as-is" can then also lift
  `pii_guard`'s `ask`. This branch is not measured on a live host.

The hook stays the source of the verdict: the mod asks `read_cost.py --verdict` and gets the
Markdown folder from `read_cost.py --target <path>`, never judging by itself. A read the engine
denies or a settings rule decides is not held. When nobody answers within one minute, when the
verdict cannot be read, or when the session draws on neither a terminal nor the desktop app
(`claude -p`, the VS Code extension alone), nothing is held and the hook's `ask` applies as is.

The mod is TypeScript, which the repo's gates do not run. Check it with
`claude plugin validate plugins/pc-read`, `claude plugin test plugins/pc-read` and
`tsc -p plugins/pc-read` (the config it extends is written by Claude Code the first time the
plugin loads from a folder you own).

## Requirements

Python 3 for the hook. `markitdown` (`uv tool install 'markitdown[all]'`) for the conversion; the
`[all]` extra pulls the docx/pptx/xlsx/pdf handlers.
