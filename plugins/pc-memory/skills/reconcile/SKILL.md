---
name: reconcile
description: Review the memory notes of the current directory level and of its parent levels with the user, note by note (promote, keep, move to another level, delete), then apply the approved plan. Use when the user asks to review, clean up or reconcile memory, or when the startup message says a review is due.
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/inventory.py:*), Bash(cat:*), Bash(rtk read:*), AskUserQuestion
---

# Reconcile

Memory is a transit zone: a note is promoted to where it belongs and deleted, or deleted. This skill
walks the notes with the user and applies what they approve. It dialogues, so it runs in the main
session. Talk to the user in the user's own language; keep file names, paths and JSON as they are.

## Promotion principle

- A skipped step of a procedure: fix the skill or hook that owns the step.
- A rule or pitfall that applies beyond this directory: add it to the source that produces the
  instructions, never to a generated or distributed copy.
- A trap local to this directory: keep the note.
- Outdated, derivable from the code, or covered elsewhere: delete.
- A pure tone or preference note is never proposed for promotion.

## Steps

1. Run `python3 ${CLAUDE_SKILL_DIR}/scripts/inventory.py` and read the JSON: one entry per level
   (current, then parents), each note with its description, age, session count, last read, index
   line and `coverage`, the orphans, the index budget and the exact duplicates. `coverage` lists up
   to 3 places (`file`, `line`, `text`, `terms`) where several rare terms of the note appear close
   together in a CLAUDE.md, an AGENTS.md, a rule, a skill, a hook description or the pinky settings
   of a level, `terms` being the words that matched. It is a clue, not a finding: it only says the
   same words appear there, and an empty list means nothing was found.
2. Judge each note with the principle above. Before proposing anything for a note, read its
   `coverage` hints. Read each `terms` list together with the cited line: shared words are not a
   shared fact. Recommend DELETE for coverage only after reading the cited line (with `cat` through
   Bash, a few lines around it) and seeing that it states the same fact as the note, then cite the
   source (path and line); never recommend keeping a note that a source already covers, and never
   recommend keep because of a vague hint. An empty `coverage` does not prove a note is not
   covered: the hints miss a fact that a source states in other words. So for every note you would
   otherwise recommend keeping, search the sources yourself first: run `grep -rn` through Bash on
   two or three distinctive words of the note over the CLAUDE.md and AGENTS.md of each level, its
   `.claude/rules/`, its skills and its hook descriptions, read the lines found, and recommend
   DELETE with the cited path and line when one of them states the same fact. Counts and ages are hints too, there is no fixed threshold, and a count is a biased proxy:
   a poorly indexed note is under-counted. Also look for near-duplicates in the index lines,
   across levels too. Read a note only when its description and index line are not enough, with
   `cat` through Bash, not the Read tool: a Read is counted as a consultation and would skew the
   very counters being reviewed. A read that is refused (a confidentiality guard) is never retried
   through another channel and never worked around with a pragma or an exception: judge from the
   description and the index line, and tell the user the note holds data shaped like a secret, a
   signal to delete it.
3. Put the decisions to the user with AskUserQuestion, at most 4 questions per call, and only where
   the choice is real. Obvious cases (covered by a source, outdated, an exact duplicate) go through
   one grouped question, not one question each.
   - When two or more notes are covered, ask ONE grouped question first. Options: delete all N
     covered notes (recommended) / go through them one by one / keep them all. The question text
     lists each note as `<short title> — <summary> — covered by <source path:line>`. Choosing "one
     by one" turns each covered note into a per-note question below.
   - Every per-note question gives, in its text: the note title, never a bare file name; its full
     clickable `~/…` path on its own line; a 1–2 line summary of what the note says, from its
     description, or read the note when the description is vague; and, when covered, the covering
     source (path:line).
   - The `header` of each question is a 1–2 word topic of the note: not the file name stem, not a
     number.
   - Before each batch, say one short intro line in plain text, such as `Batch 2/4 — 3 notes`: it
     is never cut, not even for a batch of one note.
   - Put the recommended option first with its reason in the option description. Options are few
     and concrete: keep, delete, promote (say where), move to another level. Never a long prose
     question. An answer that is empty or unusable is not a yes: ask again.

   Worked example, one note of a batch, covered by a source:

   ```
   Batch 1/2 — 2 notes
   question: "Automode location
   ~/.claude/projects/-Users-x-pinky/memory/reference-automode-settings-location.md
   Says where the automode setting lives, in the user settings rather than the project ones.
   Covered by ~/Workspaces/pinky/CLAUDE.md:42"
   header: "Automode"
   options: "Delete (recommended)" — the rule at CLAUDE.md:42 already states it
            "Keep" — the rule does not say why
            "Move to another level"
   ```

4. When every note has an answer, build the plan and get it approved before any change. Plan shape:
   `{"actions": [{"dir": "<memory dir>", "note": "x.md", "action": "delete" | "move" | "keep",
   "to_dir": "<memory dir>", "hard": false}], "reviewed_dirs": ["<memory dir>"]}`. Use the `dir`
   values the inventory printed. A delete goes to the trash folder (`"hard": false`). List in
   `reviewed_dirs` each level the user went through. Run
   `python3 ${CLAUDE_SKILL_DIR}/scripts/apply.py --dry-run --plan - <<'EOF'` then the JSON, then
   `EOF`: it changes nothing and prints every field of every action and `reviewed_dirs`. Show that
   output as it is in the approval question (AskUserQuestion), not a summary of your own. Options:
   apply this plan (recommended); apply it with permanent deletion instead of the trash for the
   notes to delete, a separate and explicit option that is never the default; change something;
   cancel. If the user changes something, rebuild the plan, run the dry run again and ask again.
5. Run exactly the plan shown: the same JSON with `--dry-run` removed (`"hard": true` only when the
   user chose permanent deletion). This command is not pre-approved: the permission prompt shows
   the command and its plan, which is the last check. Report a refusal or a failure as it is, with
   the `left` actions. After a failure, applying the plan again is allowed once the cause is fixed;
   after a run that was killed, a new plan is refused until its marker is 10 minutes old.
6. This skill edits nothing outside memory directories. A promotion is made by the session or by
   the user, outside this flow: a note to promote stays untouched in the plan. Only after the user
   says the promotion is done, propose deleting the note, and apply it the same way after approval.
7. Say what changed (moved, trashed, kept, `last_review` set) and suggest `/clear`: a finished
   reconcile has no value left in the context.
