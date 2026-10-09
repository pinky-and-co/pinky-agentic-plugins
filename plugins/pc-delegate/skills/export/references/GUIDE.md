# Working as an external agent

You are working on a repository outside Claude Code, in a web-based agentic tool session with no
local git and no ability to move files between folders. This guide is rendered as `AGENTS.md` at
the root of what you were given — read it fully before making any change.

## What to produce when you are done

A single `.zip` archive containing every file you created or modified this session — nothing else, no
deleted-file entries, no files you only read. Preserve the repository-relative path of each file
exactly inside the archive (e.g. `src/pkg/module.py`, `docs/adr/0004-x.md`) — the path inside the
archive is how your changes get placed back, there is no separate manifest to fill in. A brand-new
file is fine; do not invent a placeholder for it elsewhere.

Also include, at the root of the archive, the `EXPORTED_CARDS.txt` file you received, returned
exactly as it is — never edited, never renamed. It lists the cards this session covered, and the
reintegration reads it to know which ones to close.

Also include a `REPORT.md` at the root of the archive (not nested under any path) — what you did,
any judgment call you made and why, anything you found but left alone because it was out of scope,
and any caveat a reviewer should know before trusting the change. This is read by a human, not
applied to the repository — write it as you would a short handoff note, not as code.

## What you cannot do here — and do not need to

You cannot commit, push, run the project's own full lint/type-check/test suite, or move a file
between folders in a way that reaches back to the local filesystem. None of that is your job in
this session — it happens after your archive is reintegrated.

One exception: if a hygiene hook or a test targeting a file you touched is present under `repo/`
and your environment can execute code, run it against your change before you finish — pass the
file(s) you touched as its argument (e.g. `python3 repo/scripts/hooks/some_check.py repo/path/to/
the/file/you/touched.py`); running it with no argument tells you nothing about your change. Paste
the command, its full output, and its exit code into `REPORT.md` — this checks your own change, it
does not replace the reintegration step, which still runs the project's own checks in full. If it
fails, fix the file you touched, not the hook or test itself, unless editing that hook or test is
what your task actually asked for — otherwise it was shipped to verify your change, not to be
edited by it.

Write clean code by the conventions already visible in the files you touch; the reintegration step
runs the project's own checks and will flag anything mechanical or ask a human about anything
requiring judgment.

## Scope discipline

Touch only the files needed for the task you were given. A file outside that scope, even if it
looks related, is out of bounds — note what you think should change and why instead of changing it
yourself.

## Project-specific conventions

The card(s) and any accompanying context included alongside this guide state the actual task and
its constraints — read those first. This guide covers only the mechanics of working outside Claude
Code and handing results back; it carries no project-specific convention on its own.
