---
name: council
description: "Run a question or decision through a council of 5 advisors on 5 models: independent analysis, anonymous peer review, a chairman's verdict cross-checked on one more model. Takes a type: `generic` (any decision), `tech` (architecture, writes an ADR), `pitch` (product concept, writes founding docs). TRIGGERS: 'council this', 'run the council', 'war room this', 'pressure-test this', 'stress-test this', 'debate this', 'architecture council', 'tech council', 'ADR this', 'pitch council', 'concept council'; also 'should I X or Y', 'which option', 'which design', 'which approach', 'is this the right move', 'validate this', 'I'm torn between', 'get multiple perspectives'. Do NOT trigger on yes/no questions, factual lookups or a reversible one-line choice."
argument-hint: "[generic|tech|pitch] <the question, decision, or concept>"
metadata:
  origin: "Andrej Karpathy's LLM Council (x.com/karpathy); pinky adaptation — cross-model advisors per pinky-bootstrap ADR-0006"
  updated: "2026-08-27"
---

# Council

The council runs the question through 5 advisors, each on a **different model family** and each
thinking from a different angle, has them peer-review each other anonymously, then a chairman
synthesises a verdict: where they agree, where they clash, what to do.

Cross-model dispatch runs through the `panel` MCP tool (`pc-core-models` server); when the pool
can't field enough distinct families, the unfilled lenses fall back to Claude sub-agents, **traced
as such** in the transcript.

---

## The `type` argument

Resolve it in this order:

1. **Explicit** — the first token of the argument is `generic` / `tech` / `pitch`. Use it.
2. **Content** — no explicit token: read the question. An architecture or implementation choice →
   `tech`. Whether to build a product/tool concept → `pitch`. Anything else → `generic`.
3. **Trigger phrase** — only as a tie-breaker when content is genuinely unclear (`ADR this` → `tech`;
   `pitch council` → `pitch`; else `generic`).

Content beats the trigger phrase. When the phrase and the content disagree, **say which type you
picked and why in one line before Step 1**. If still unsure after all three, ask — one question.

| type | for | typed output |
|---|---|---|
| `generic` | any decision with stakes and no one defensible answer | an HTML report + the transcript |
| `tech` | an architecture / technical choice | `docs/adr/NNNN-<slug>.md` in the target repo |
| `pitch` | whether to build a product concept | `explanation/1-design.md` + `2-features.md` |

**Once the type is resolved, read `references/type-<type>.md` in this skill's directory.** It carries
every delta from the shared flow below: extra context to scan, the type's fourth gate check, the
prior-art focus, lens framing, the chairman's extra sections, and the output writer.

---

## Preflight — run these three checks before the gate

1. **`panel` reachable?** If the `panel` MCP tool (`pc-core-models` server) is not in your tool list,
   you are in **full-fallback mode**: every advisor runs as a Claude sub-agent, the council has
   **zero cross-model signal at the advisor layer**, and Step 6 (chairman cross-check) stops being
   optional — it is then the *only* cross-model input and must run. Say so up front. (`adversarial` and `offline` are *not* substitutes for `panel`
   — see Step 3 / Step 6.)
2. **Sub-agent spawning allowed?** Run it from the **main session** (~12 sub-agent/model calls;
   a sub-agent hits sub-agent-of-sub-agent limits at the Step 3 fallback and Step 4). If spawning is restricted here, stop and say so.
3. **Interactive user present?** If not (you are a sub-agent, or a scheduled run): skip the Step 1
   clarifying question (frame best-effort), and the Step 3 egress gate defaults to **withheld**
   (Claude-only) unless the invoking context explicitly pre-approved egress.

---

## When to run — the gate

The council costs ~12 sub-agent/model calls, roughly 10× a direct answer. Run this gate before step 1.

**Gate check 1 — explicit invocation.** If the user typed `/council` or used a MANDATORY TRIGGER,
skip the rest and go to step 1. They opted in.

**Gate check 2 — self-judge** (only if check 1 did not match). Any "no" aborts:

1. **Genuinely open?** Would five competent people give materially different answers? One defensible
   answer → abort.
2. **Expensive to be wrong?** A public API surface, a product bet, an irreversible migration, a name
   that ships → yes. A reversible one-line choice → abort.
3. **Open phrasing?** Did the user avoid "quick", "just", "simply", "standard", "canonical",
   "en deux mots"? Any of those → they want the direct answer → abort.

**Gate check 3 — the type's own check.** From `references/type-<type>.md` (e.g. `tech`: is an
accepted ADR already deciding this?).

**On abort:** answer directly, then offer once, in one sentence — *"if you want this pressure-tested
by 5 advisors on 5 models with anonymous peer review, say `council this`."* Never run the council to
look thorough.

---

## The five advisors

Thinking styles, not job titles. `references/type-<type>.md` reframes each for the type.

1. **The Contrarian** — hunts for what's wrong, missing, or will fail. Assumes a fatal flaw and
   tries to find it.
2. **The First Principles Thinker** — ignores the surface question, rebuilds from the ground up.
   Sometimes the answer is "you're asking the wrong question."
3. **The Expansionist** — looks for the upside everyone else is missing. What if this works better
   than expected?
4. **The Outsider** — zero context on the project, field, or history. Reacts only to the bare
   decision, in plain language. This is the one lens that does **not** get the framed question's
   context block or the prior-art summary (Step 3) — feeding it those would defeat the point.
   Catches the curse of knowledge and unspoken jargon.
5. **The Executor** — one question only: can this be done, and what's the fastest path? Ignores
   theory.

---

## Session

### Step 1 — resolve type, frame the question

Read `references/type-<type>.md`. Then scan for context: workspace `CLAUDE.md` / `AGENTS.md`, any
`memory/` folder, files the user referenced, and — recursively from the workspace root —
`council-transcript-*.md` (don't re-council settled ground), **plus the type's own additions** from
the reference. Reframe the user's raw question into one neutral prompt every advisor will receive:
the core decision, key context (from the message and the scanned files), and what's at stake. Don't
add your own opinion. Save the framed question for the transcript. Too vague ("council this: my
business") → ask one clarifying question, then proceed (skip the question on an unattended run — see
Preflight).

### Step 2 — prior-art scan

Spawn one Claude research sub-agent: **does this already exist?** PyPI, GitHub, the platform's native
features, named patterns. Focus per the type reference. Output: a **Prior Art Summary** whose
headline is **at most 150 words** — what exists, what's maintained vs abandoned, the genuine gap;
package names, links and a short sources list may follow as a bulleted appendix. Appended to every
advisor's prompt **except the Outsider's** (Step 3).

### Step 3 — convene the council (5 advisors)

**Egress gate — the framed question + prior art carry workspace context off the machine.**

- If the subject falls in a scope the workspace treats as sensitive (employer-derived material,
  personal data, anything the workspace's own instructions mark confidential) → **do not route**. Run all 5 lenses as
  Claude sub-agents. Note "egress: withheld (sensitive scope)" in the transcript. Do not ask.
- Otherwise → ask the user once: *"The council will send the framed question to third-party model
  providers in the pool (`panel` tool). OK to proceed, or run Claude-only?"* Refusal → all 5 lenses
  as Claude sub-agents, note "egress: declined".

**If cleared:** build the 5 lens-prompts **in lens order** — Contrarian 1, First Principles 2,
Expansionist 3, Outsider 4, Executor 5. Prompts 1, 2, 3, 5 = the lens instruction + framed question +
prior-art summary. **Prompt 4 (Outsider) = the lens instruction + the bare decision restated in
plain language only** — no context block, no prior art (see § The five advisors). Each response
150–300 words, "be specific, name what WILL break or WIN, don't hedge". Call the `panel` MCP tool
with the 5 prompts as `tasks`; it pairs prompt *i* with the *i*-th model, so **slot *i* is always
lens *i*** even when a slot drops out. From its output:

- each `--- [slot N · <model>] ---` block → that is lens *N*'s response, from `<model>`.
- `skipped: slot N <model> (<reason>)` → lens *N* failed on a model → run **lens *N* only** as a
  Claude sub-agent with the same lens-prompt.
- `unfilled: slot(s) N, …` → the pool had fewer families than 5 → run **those lenses** as Claude
  sub-agents.

**Full fallback** (gate withheld/declined, or `panel` returns BLOCKED): all 5 as Claude sub-agents.

Record `lens → model → model-version → tier` for every slot (a Claude persona is
`lens → claude-persona`). This log is mandatory (transcript).

### Step 4 — peer review (5 sub-agents, parallel)

Anonymise the 5 responses as A–E in random order — **identically whether a response came from a
routed model or a Claude persona; never reveal the source**. Spawn 5 Claude sub-agents, each
answering: (1) strongest response and why? (2) biggest blind spot and what is it? (3) what did ALL
five miss?

### Step 5 — chairman synthesis (this session)

You are the chairman. Not delegated. Input: the framed question, the 5 de-anonymised responses (lens labels only, never
models), the 5 peer reviews. Produce, in this exact order:

```
## Where the Council Agrees
[points ≥2 advisors converged on independently — high-confidence signals]

## Where the Council Clashes
[real disagreements — both sides, why reasonable advisors differ]

## Blind Spots the Council Caught
[what only surfaced in peer review]

## The Recommendation
[a clear, direct answer — not "it depends". The chairman may side with a lone dissenter if the
 reasoning is strongest.]

## The One Thing to Do First
[a single concrete next step]
```

Plus any type-specific section from `references/type-<type>.md` (e.g. `pitch`: Go / No-Go /
Conditional Go).

### Step 6 — chairman cross-model check

"Where the Council Agrees" measures the shared prior of the advisor models as much as the argument.
The chairman is one model. Call the `panel` tool with **a single
task** — the exact chairman prompt from step 5 (framed question + 5 responses + 5 peer reviews) —
so one more distinct model produces its own verdict. Diff the two and append:

```
## Where the Two Chairmen Diverge
[Same recommendation → say so: cross-model convergence, a real signal. Different → THIS is the
 finding; give both verdicts and name where the reasoning splits. It carries into the written output.]
```

**Do not substitute `adversarial` for `panel` here** — it frames its output as a critique, not an
independent verdict, and would confound the diff.

**Blocking depends on the run.** In a normal run (`panel` fielded routed advisors), this is
optional: `panel` BLOCKED / every candidate failed → record "cross-model check unavailable" and
continue. In a **full-fallback run** (Preflight check 1, or every advisor fell back), this is the
*only* cross-model signal in the whole council — do not skip it, and if it truly cannot run, say
plainly in the output that the verdict rests on a single model end to end.

### Step 7 — write the output

Per `references/type-<type>.md`. **Always** also write the transcript (below), whatever the type.

---

## Transcript — always

`council-transcript-<timestamp>.md` where `<timestamp>` is `YYYYMMDD-HHMMSS`. Put it next to the
typed output for `tech` / `pitch`; for `generic`, in a `council/` folder at the workspace root
(create it) — unless a scan shows the workspace already keeps council transcripts elsewhere, in
which case follow that. It carries:

- the original and the framed question
- the Prior Art Summary
- all 5 advisor responses
- all 5 peer reviews, with the A–E → lens mapping revealed
- the chairman synthesis, the cross-model verdict, and the diverge/converge note
- the assignment log: `lens → model → version → tier` for every slot, and any `unfilled`/`skipped`

---

## Rules

1. **Spawn the 5 advisors (or the fallback sub-agents) and the 5 peer reviewers in parallel** — never
   sequentially; earlier answers must not bleed into later ones.
2. **Anonymise for peer review, uniformly** — reviewers must not know which response came from which
   lens, which model, or whether it was a routed model at all.
3. **The chairman sees lenses, never models** — the model↔lens mapping lives only in the transcript
   (ADR-0006: a divergence stays attributable, never confounded with a quality gap).
4. **The chairman can disagree with the majority** if the minority reasoning is stronger.
5. **Agreement among voices is not proof** — that is what step 6 is for. Never drop it silently; if
   it could not run, say so in the output and the transcript.
6. **Don't council the trivial.** One right answer → just answer it.
