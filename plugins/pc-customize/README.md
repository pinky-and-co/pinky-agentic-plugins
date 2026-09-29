# pc-customize

Appearance and response-style customization for Claude Code, shipped as a plugin.

## Contents

- `output-styles/*.md` — output styles, auto-loaded from the plugin root when the plugin is enabled.
- `themes/*.json` — color themes (a `base` preset plus a sparse `overrides` map), listed in `/theme`.

## Activation

Enable the plugin at **user** scope so the styles and themes apply in every project. Styles and themes
are only available where the plugin is enabled.

## Selection

No `force-for-plugin` on any style yet: pick the style in `/config` and the theme in `/theme`.
