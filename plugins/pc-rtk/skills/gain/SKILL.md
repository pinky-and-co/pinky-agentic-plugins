---
name: gain
description: Show RTK's token-savings summary — commands filtered, tokens saved, per-command breakdown. Read-only. Use when the user asks how much RTK saved, or whether the pc-rtk rewrite hook is taking effect.
argument-hint: "[-p|--project] [-g|--graph] [-H|--history] [-q|--quota] [-t|--tier <tier> (only with -q)] [-d|--daily] [-w|--weekly] [-m|--monthly] [-a|--all] [-f|--format <fmt>] [-F|--failures] [--recalls]"
model: haiku
context: fork
agent: general-purpose
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/skills/gain/scripts/gain.sh:*), Bash(rtk gain)
---

# RTK token savings

Run the script and reproduce its stdout **verbatim** in one fenced code block — it is already the
complete report. Add nothing: no lead-in, no recap, no recomputed figure.

```bash
${CLAUDE_PLUGIN_ROOT}/skills/gain/scripts/gain.sh [<options>]
```

`args` is whatever trails an `ARGUMENTS: ...` line at the very end of this prompt; no such line
means no argument, which is the normal bare invocation. Pass the arguments through unchanged — they
are the read-only `rtk gain` options listed in `argument-hint`. The script refuses any other argument
(`--reset`, `--yes`…) with one line and exit 1, without calling `rtk`; report that line, never retry
with another form.

A non-zero exit means the run failed: reproduce the script's output and the exit code. Exit 1 with a
`brew install rtk` line means RTK is not installed — say so, never describe it as zero savings.
