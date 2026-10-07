export type HeldRead = {
  id: string
  file: string
  path: string
  // `pii` : des colonnes sensibles ; `oversize` : fichier non analysé, trop gros.
  kind: 'pii' | 'oversize'
  columns: string[]
  // Une copie anonymisée est proposable.
  anonymize: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'pc-privacy': {
      held: HeldRead | null
      // Le dossier du plugin, pour qu'un autre plugin lance `pii_guard.py` sans deviner où il vit.
      root: string
    }
  }
}
