---
name: yt
description: "Summarize a YouTube video (any source language — auto-detected) and produce a relevance verdict against the active chantier board — the verdict is the feature, not the summary. Deterministic fetch (yt-dlp → VTT → clean text), analysis reserved for the LLM (fetch/LLM boundary). MANDATORY TRIGGER: '/yt'. STRONG TRIGGERS: 'summarize this YouTube video', 'is this video relevant to what we're doing', 'check if this video brings anything new'. Do NOT trigger for downloading/transcoding video, or for non-YouTube sources — this skill is summary + board-relevance only."
argument-hint: "<url> [lang] [--card]"
allowed-tools: Bash(yt-dlp:*), Bash(python3:*), Bash(ls:*), Bash(rtk ls:*), Read, Write, AskUserQuestion
---

# /yt — YouTube summary + relevance verdict

## Arguments

`/yt <url> [lang] [--card]`

- `<url>` — YouTube URL (required, first token).
- `[lang]` — **optional override** for the source subtitle language. Default: absent — the language is
  **auto-detected from the video itself** (step 1); the video can be in any language — never assume a
  default. Only pass `[lang]` to force a specific track (e.g. detection is wrong, or you deliberately
  want the translated track instead of the original).
- `[--card]` — file the verdict into a `0-ideas` card of the relevant chantier, instead of the default
  chat-only output.

## Requirements

`yt-dlp` on `PATH` — provisioned by the workspace setup (derived from `allowed-tools`). If it is
absent the fetch `Bash` call fails with `command not found` — report that and stop, do not work
around it.

## Fetch / LLM boundary

The fetch (yt-dlp → VTT → clean text) is **deterministic, never an agent** — the LLM only handles the
analysis that follows (summary + verdict). Never read the raw `.vtt` into context: always go through the
cleanup step first.

## Step 1 — language detection + deterministic fetch

**Never hardcode a subtitle language** — YouTube exposes dozens of machine-translated "automatic
captions" tracks, and a blind fetch risks grabbing one instead of the native track.

**And never trust the `language` metadata field as a track name.** It is a *declaration* (`en-US`),
not a track code; YouTube names the native track `<base>-orig` (`en-orig`, `fr-orig`) and reserves the
bare code (`en`) for the machine re-translation. Passing `en-US` to `--sub-lang` yields
`There are no subtitles for the requested languages`. Always resolve the declaration **against the
tracks the video actually carries**, in one `--dump-json` call:

```bash
yt-dlp --dump-json --skip-download "<url>" | python3 -c '
import json, sys
d = json.load(sys.stdin)
tracks = set(d.get("subtitles") or {}) | set(d.get("automatic_captions") or {})
meta = (d.get("language") or "").strip()
orig = sorted(t for t in tracks if t.endswith("-orig"))
if orig:                                   # native track, whatever the metadata says
    print(orig[0])
elif meta in tracks:                        # exact metadata code
    print(meta)
elif meta.split("-")[0] in tracks:          # base code (en-US -> en)
    print(meta.split("-")[0])
else:
    print("")                               # nothing usable -> stop, report to user
'
```

- If `[lang]` was passed by the user → it wins, skip resolution entirely.
- Empty output → no usable track (declared language absent from the track list, or no captions at
  all). Report it and stop — do not fall back onto an unrequested translated track.
- The `-orig` suffix wins over the metadata **by design**: it is the audio's own transcription.

```bash
yt-dlp --write-auto-sub --skip-download --sub-lang "<resolved_track>" \
  --output "<scratchpad>/yt_<slug>" "<url>"
```

Use the session's scratchpad directory (never `/tmp`). If no `.vtt` file is produced (no captions in
that language, private/restricted video) — report it to the user and stop, no fallback onto an
unrequested translated track.

## Step 2 — cleanup (strip timestamps + dedup)

```bash
python3 - "<scratchpad>/yt_<slug>.<resolved_track>.vtt" <<'PYEOF'
import re, sys
with open(sys.argv[1]) as f:
    content = f.read()
seen, out = set(), []
for line in content.split('\n'):
    line = line.strip()
    if not line or '-->' in line or line.startswith(('WEBVTT', 'Kind:', 'Language:')):
        continue
    clean = re.sub(r'<[^>]+>', '', line).strip()
    if clean and clean not in seen:
        seen.add(clean)
        out.append(clean)
print('\n'.join(out))
PYEOF
```

Redirect the output to `<scratchpad>/yt_<slug>.txt` — this clean text file is what gets read into
context, never the `.vtt`. Delete the `.vtt` files after reading the clean text.

## Step 3 — analysis (summary then verdict — the verdict IS the feature)

1. Read the clean text — **regardless of its source language** (the transcript can be in English,
   Spanish, Japanese... any language).
2. **Short summary** — 3 to 5 key points, 2-3 sentences of context/subject. Produce the summary and
   the verdict in your normal working language for this session, whatever the video's language.
3. **Relevance verdict against the board.** Resolve the board root(s) visible to this session — do
   not hardcode a path:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-chantiers --full
   ```

   `--full` is required here — the verdict needs the full Statut prose, not the bounded default
   (which caps each Statut cell). That prints each board's `INDEX.md` verbatim plus its
   chantier list. Then, for the chantiers the video's topic touches, read the active cards:

   ```bash
   ls <board-root>/CHANTIER*/3-in-progress/ <board-root>/CHANTIER*/2-selected-for-dev/
   ```

   Conclude explicitly with one of these three forms:
   - "relevant to `<card/chantier>` because…"
   - "contradicts `<ADR/decision>`" (name the decision and the divergence)
   - "nothing new for us"

Don't stop at the summary — a `/yt` that doesn't render a verdict hasn't done its job.

## Step 4 — output

- Default: chat only (summary + verdict displayed, nothing written to disk outside the scratchpad).
- `--card`: write the verdict into a new `0-ideas/` card of the relevant chantier (date it
  `· surfacé YYYY-MM-DD`, see the `kanban` skill for the card frontmatter schema) — ask which
  chantier if ambiguous, never guess silently. Use the board root resolved in step 3, not a relative
  path.

## Notes

- Cross-link: `pc-read:document-to-md` (same convert-then-read / token economy spirit, different
  format).
