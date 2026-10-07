export type HeldRead = {
  id: string
  path: string
  // Les lignes que la garde signale (1-based), non vide.
  lines: number[]
  // Le condensat du contenu jugé : le pragma refuse un fichier qui a changé depuis.
  digest: string
  // Un `#` en fin de ligne garde le fichier valide.
  pragma: boolean
  // Le pool local répond : `offline` est proposable.
  offline: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'pc-secrets': { held: HeldRead | null }
  }
}
