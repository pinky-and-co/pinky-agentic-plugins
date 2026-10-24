# pc-customize

Appearance and response-style customization: output styles and color themes.

## Overview

The plugin ships two kinds of appearance assets, auto-loaded from the plugin root when the plugin is
enabled: `output-styles/*.md` — one Markdown prompt file per output style (`Skim-and-push` is the
current one) — and `themes/*.json` — color themes (`lisible` is the current one), each combining a
`base` preset with a sparse `overrides` map and listed in `/theme`. Enable the plugin at **user**
scope so the styles and themes apply in every project; they are only available where the plugin is
enabled. No `force-for-plugin` is set on any style: pick the style in `/config` and the theme in
`/theme`.

## Skills

None — the plugin ships no skills; appearance is delivered through the output styles and theme
described above.

## Requirements

None — the plugin contains no scripts; it ships a Markdown output style and a JSON theme that
Claude Code itself interprets.

## Dependencies

None.
