export type PiRun = {
  // When the call started, by the engine's clock.
  startedAt: number
  // When the call ended; the view stays drawn a few seconds past it.
  endedAt?: number
  // One entry per `pi` stream (a panel has several): its header, then its last lines.
  views: string[][]
}

declare module 'claude-code' {
  interface PluginState {
    'pc-models': { piRuns: Record<string, PiRun> }
  }
}
