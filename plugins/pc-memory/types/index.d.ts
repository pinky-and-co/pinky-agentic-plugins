export type MemoryLevel = {
  name: string
  dir: string
  current: boolean
  target: boolean
}

export type HeldWrite = {
  id: string
  description: string
  file: string
  lines: number
  // Le niveau visé d'abord, puis les autres niveaux où la note peut aller ; vide sans `levels.py`.
  levels: MemoryLevel[]
  relative: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'pc-memory': { held: HeldWrite | null }
  }
}
