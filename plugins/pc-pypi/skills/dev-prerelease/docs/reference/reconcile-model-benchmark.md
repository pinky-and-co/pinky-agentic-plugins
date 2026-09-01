---
title: Reconcile benchmark
---

# Reconcile model benchmark

*Update date : 2026-08-31 19:41*

The `dev-prerelease` Step 0b model pass is **advisory**. This records which model + brief clears the
reconcile oracle (`../../evals/reconcile_oracle.py`, 2 frozen fixtures) and which do not, so the
skill's transport order and the "advisory, verify by hand" framing are grounded, not guessed.

Re-run: `python3 evals/reconcile_oracle.py --transport {offline|panel} [--model NAME] --brief signature`
(scores every fixture; `--brief both` also runs the `all` / `selective` baselines).

## Scoring

- **recall** — `recall_broken_promises`: 3 resource classes marked delivered in the table are bare
  stubs (`⚠ STUB`). Catching all 3 = 3/3. Missing any = a rubber-stamp.
- **precision** — `precision_clean_table`: an honest table + module. Correct output is near-silence;
  the anchor is that `purge_expired` (already a row) must **not** be demanded as missing.

## The pick

| Leg | Model | Transport | Brief | recall | precision |
|---|---|---|---|---|---|
| **primary** | `gemma4:12b` (local) | `offline` | `signature` | **3/3** (7 runs) | **clean** (7 runs) |
| **fallback** | `mistral-medium-3.5` | `panel`, model pinned | `signature` | **3/3** | **clean** |
| last resort | — | — | — | reconcile skipped, release continues |

The `panel` default selection (`cohere-command-a`) is **not** used — it fails both axes. The
fallback pins `mistral-medium-3.5` explicitly.

## Why the `signature` brief

The brief is the feature table + every public symbol's signature + first docstring line + a
deterministic `⚠ STUB` flag (AST: body is only a docstring / `pass` / `...` /
`raise NotImplementedError`). It is what makes a small local model viable:

| Brief | what it sends | `gemma4:12b` recall | size on a real lib |
|---|---|---|---|
| `signature` | signatures + docstrings + stub flags | **3/3** (7 runs) | 5–11 K chars |
| `all` | every full body | 3/3 (2 runs) | 90–136 K chars — overflows `num_ctx` |
| `selective` | package index + referenced full bodies | **0/3** (2 runs) — rubber-stamp | ~2–3 K chars |

`all` works only because a fixture is tiny; on a real lib it exceeds the local model's context and
truncated code reads as absent code. `selective` fits but a small model rubber-stamps it. The
`⚠ STUB` flag is what carries recall — it hands the model the one fact a docstring can still lie
about.

## Models that do not clear the oracle

| Model | tier | brief | result |
|---|---|---|---|
| `cohere-command-a` | opus | `signature` | recall 0/3 · flags `purge_expired` |
| `mistral-medium-3.5` | opus | `all` | recall 2/3 (misses `grant`) |
| `qwen2.5-coder:14b` (local) | — | `selective` | recall 1/3 |
| `lfm-2.5` | haiku | `all` | recall 1/3 · flags `database` |
| `cohere-command-r7b` | haiku | any | HTTP 400 — does not run |

No `haiku`-tier HTTP model in the pool clears it. The fallback is `opus`-tier (`mistral-medium-3.5`),
not `haiku` — the original card's `panel tier=haiku` plan is dead.
