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

Also include a `REPORT.md` at the root of the archive (not nested under any path) — what you did,
any judgment call you made and why, anything you found but left alone because it was out of scope,
and any caveat a reviewer should know before trusting the change. This is read by a human, not
applied to the repository — write it as you would a short handoff note, not as code.

## What you cannot do here — and do not need to

You cannot commit, push, run the project's own lint/type-check/test suite, or move a file between
folders in a way that reaches back to the local filesystem. None of that is your job in this
session — it happens after your archive is reintegrated. Write clean code by the conventions
already visible in the files you touch; the reintegration step runs the project's own checks and
will flag anything mechanical or ask a human about anything requiring judgment.

## Scope discipline

Touch only the files needed for the task you were given. A file outside that scope, even if it
looks related, is out of bounds — note what you think should change and why instead of changing it
yourself.

## Project-specific conventions

The card(s) and any accompanying context included alongside this guide state the actual task and
its constraints — read those first. This guide covers only the mechanics of working outside Claude
Code and handing results back; it carries no project-specific convention on its own.
