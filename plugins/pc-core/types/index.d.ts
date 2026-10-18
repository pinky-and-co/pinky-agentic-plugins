export type RoutedChoice = {
  model: string
  effort: string
}

export type HeldAgent = {
  id: string
  // The agent type the call names; `general-purpose` when it names none.
  agent: string
  // The grid category behind the proposal, or `card` when the card supplied both values.
  category: string
  proposal: RoutedChoice
  // What the keys have picked so far; starts as the proposal.
  chosen: RoutedChoice
}

declare module 'claude-code' {
  interface PluginState {
    'pc-core': { held: HeldAgent | null }
  }
}
