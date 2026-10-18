// The workspace's tier marks, one per model alias: shared by the subagent band and the board pane.
export const TIERS: Record<string, string> = { haiku: '🟦', sonnet: '🟪', opus: '🟧' }

export function tierMark(model: unknown): string {
  return typeof model === 'string' ? (TIERS[model] ?? '⬜') : '⬜'
}
