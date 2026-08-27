# Council type: `generic`

Any decision with real stakes and no single defensible answer. The default type.

## Step 1 — extra context to scan

- recent `council-transcript-*.md` in the workspace — don't re-council ground already covered.
- if the question touches a stack the workspace documents (Snowflake, a framework), read the 2–3
  relevant knowledge skills/files and fold their **constraints and prior decisions** into the framed
  question as a `Technical Context` block. Advisors may challenge those decisions, but must engage
  with the reasoning, not ignore it.

## Gate check 3 (the type's own)

None beyond the shared gate. `generic` is the catch-all; if it were a settled question the shared
check 2.1 already caught it.

## Prior-art focus (step 2)

The broad ecosystem: PyPI, GitHub, native platform features, named methodologies. "Does a tool or an
established practice already solve this?"

## Advisor framing (step 3)

The five lenses as written in `SKILL.md`, applied to the decision as posed. No domain reframing.

## Chairman — extra sections (step 5)

None. The shared five sections are the whole verdict.

## Step 7 — output

1. **`council-report-<timestamp>.html`** in the workspace — one self-contained file, inline CSS,
   system font stack, light background, subtle borders. Contains: the question at the top; the
   chairman verdict prominently (including the cross-model check, or a note it was unavailable); a
   simple agreement/divergence visual (grid or spectrum showing where advisors aligned); collapsible
   sections for each advisor's full response and for the peer-review highlights; a footer with the
   timestamp and what was counciled. Open it after writing.
2. The transcript (always — see `SKILL.md`).
