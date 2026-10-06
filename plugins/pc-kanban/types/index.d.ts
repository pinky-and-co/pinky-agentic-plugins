// Contract between the board pane and its Python data source (hooks/board-pane/board_data.py).
export type BoardCard = {
  slug: string
  // `repos:` as stop_handoff.card_repos reads it; null when the card declares none.
  repos: string[] | null
  board: string
  chantier: string
  chantier_id: string | null
  column: string
  path: string
  title: string
  frontmatter: Record<string, unknown>
  priority: number
  body: string
  story: string | null
  age_days: number | null
}

export type SessionRepos = { in_repo: boolean; repos: string[] }

export type SearchHit = { scope: string; path: string; rel: string; line: number; text: string }

export type SearchResult = {
  query: string
  counts: Record<string, number>
  hits: SearchHit[]
  truncated: boolean
}

export type Screen = 'list' | 'card' | 'story' | 'search'

// The search screen's two modes: the query field, or the listed results with a cursor.
export type SearchMode = 'edit' | 'results'

export type BoardState = {
  screen: Screen
  // Index into the four shown columns.
  tab: number
  cursor: number
  scroll: number
  // The card shown by the card and story screens: board, chantier and slug, none of which a move between columns changes.
  cardKey: string | null
  cards: BoardCard[]
  session: SessionRepos | null
  isLoaded: boolean
  // Output of the last action (the script's own words on a refusal).
  notice: { kind: 'info' | 'error'; text: string } | null
  storyText: string | null
  search: SearchResult | null
  searchMode: SearchMode
  // Where the search screen came from, and the list cursor to give back.
  searchFrom: { screen: Screen; cursor: number }
  scopes: string[]
  isDropping: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'pc-kanban': { board: BoardState }
  }
}
