export type HeldRead = {
  id: string
  file: string
  path: string
  // `markitdown` is on the PATH: a conversion is proposable.
  convert: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'pc-read': { held: HeldRead | null }
    // Publié par `pc-privacy` : le dossier où vit sa garde `pii_guard.py`. Doit rester identique au contrat de `pc-privacy`.
    'pc-privacy': { root: string }
  }
}
