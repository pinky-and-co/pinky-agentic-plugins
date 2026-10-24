// The `/board` pane: the kanban cards of the session's repos, browsable and actionable. Every card fact comes
// from board_data.py; this module only filters, sorts and draws.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { BoardCard, BoardState, SearchHit, SearchResult, SessionRepos } from '../../types'

const PANE = 'board'
const DATA = 'hooks/board-pane/board_data.py'
const CARD_OPS = 'hooks/card-ops/pc_kanban_card_ops.py'
const PAGE = 10
const MAX_COUNT = 99
const MAX_NOTICE_LINES = 6
// Control characters, ANSI sequences and bidi/format characters: a card is text the pane does not author.
const UNSAFE = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

export const TABS = [
  { column: '0-ideas', emoji: '💡', name: 'ideas' },
  { column: '1-todo', emoji: '📋', name: 'todo' },
  { column: '2-selected-for-dev', emoji: '📌', name: 'selected' },
  { column: '3-in-progress', emoji: '⏳', name: 'in progress' },
] as const

const PRIORITY_MARKS: Record<number, { glyph: string; color: string }> = {
  1: { glyph: '▼', color: 'blue' },
  2: { glyph: '▽', color: 'blue' },
  3: { glyph: '=', color: 'yellow' },
  4: { glyph: '△', color: 'red' },
  5: { glyph: '▲', color: 'red' },
}

const TIERS: Record<string, string> = { haiku: '🟦', sonnet: '🟪', opus: '🟧' }

export const SCOPES = ['kanban', 'archive', 'logs'] as const

export const initialState: BoardState = {
  screen: 'list',
  tab: 0,
  cursor: 0,
  scroll: 0,
  cardKey: null,
  cards: [],
  session: null,
  isLoaded: false,
  notice: null,
  storyText: null,
  search: null,
  searchMode: 'edit',
  searchFrom: { screen: 'list', cursor: 0 },
  scopes: [...SCOPES],
  isDropping: false,
}

const board = atom({ plugin: 'pc-kanban', key: 'board' } as const, initialState)

// ── Text measures ────────────────────────────────────────────────────────────

// Cells a character takes in a terminal: emoji and CJK take two, a variation selector 16 widens the character
// before it to two, the arrows and triangles of the priority marks take one.
const WIDE: ReadonlyArray<readonly [number, number]> = [
  [0x1100, 0x115f], [0x231a, 0x231b], [0x23e9, 0x23f3], [0x25fd, 0x25fe], [0x2614, 0x2615], [0x2648, 0x2653],
  [0x267f, 0x267f], [0x2693, 0x2693], [0x26a1, 0x26a1], [0x26aa, 0x26ab], [0x26bd, 0x26be], [0x26c4, 0x26c5],
  [0x26ce, 0x26ce], [0x26d4, 0x26d4], [0x26ea, 0x26ea], [0x26f2, 0x26f3], [0x26f5, 0x26f5], [0x26fa, 0x26fa],
  [0x26fd, 0x26fd], [0x2705, 0x2705], [0x270a, 0x270b], [0x2728, 0x2728], [0x274c, 0x274c], [0x274e, 0x274e],
  [0x2753, 0x2755], [0x2757, 0x2757], [0x2795, 0x2797], [0x27b0, 0x27b0], [0x27bf, 0x27bf], [0x2b1b, 0x2b1c],
  [0x2b50, 0x2b50], [0x2b55, 0x2b55], [0x2e80, 0xa4cf], [0xac00, 0xd7a3], [0xf900, 0xfaff], [0xfe30, 0xfe6f],
  [0xff00, 0xff60], [0xffe0, 0xffe6], [0x1f300, 0x1faff], [0x20000, 0x3fffd],
]

export function displayWidth(text: string): number {
  let width = 0
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0
    if (code === 0xfe0f) width += 1 // emoji presentation of the previous, narrow, character
    else if (code === 0xfe0e || code === 0x200d || (code >= 0x300 && code <= 0x36f)) width += 0
    else width += WIDE.some(([lo, hi]) => code >= lo && code <= hi) ? 2 : 1
  }
  return width
}

export function clean(text: string): string {
  return text.replace(UNSAFE, ' ').replace(/\s+$/, '')
}

// Cuts `text` to `width` cells, ending in an ellipsis when something is left out.
export function fit(text: string, width: number): string {
  if (displayWidth(text) <= width) return text
  let out = ''
  for (const char of text) {
    if (displayWidth(`${out}${char}…`) > width) break
    out += char
  }
  return `${out.trimEnd()}…`
}

// Hard word wrap on display width; a word wider than the line is cut.
export function wrapText(text: string, width: number): string[] {
  const room = Math.max(1, width)
  if (displayWidth(text) <= room) return [text]
  const lines: string[] = []
  let line = ''
  for (const word of text.split(' ')) {
    let rest = word
    while (displayWidth(rest) > room) {
      if (line !== '') {
        lines.push(line)
        line = ''
      }
      let piece = ''
      for (const char of rest) {
        if (displayWidth(`${piece}${char}`) > room) break
        piece += char
      }
      lines.push(piece)
      rest = rest.slice(piece.length)
    }
    const next = line === '' ? rest : `${line} ${rest}`
    if (displayWidth(next) > room) {
      lines.push(line)
      line = rest
    } else line = next
  }
  lines.push(line)
  return lines
}

// A title in at most two lines, the second one cut when the title is longer.
export function titleLines(title: string, width: number): string[] {
  const lines = wrapText(clean(title), width)
  if (lines.length <= 2) return lines
  return [lines[0] ?? '', fit(lines.slice(1).join(' '), width)]
}

// ── Tab bar ──────────────────────────────────────────────────────────────────

export function tabLabel(index: number, count: number): string {
  const tab = TABS[index]
  const shown = count > MAX_COUNT ? `${MAX_COUNT}+` : String(count)
  return tab === undefined ? '' : `${tab.emoji} ${tab.name} (${shown})`
}

export const TAB_GAP = 2

// The tab bar's rows, each a list of tab indexes: the packing the bar is drawn with and the list's row count uses.
export function tabRows(counts: readonly number[], columns: number): number[][] {
  return packRows(TABS.map((_, i) => displayWidth(tabLabel(i, counts[i] ?? 0))), columns - 1, TAB_GAP)
}

export function tabBarWidth(counts: readonly number[]): number {
  const labels = TABS.map((_, i) => displayWidth(tabLabel(i, counts[i] ?? 0)))
  return labels.reduce((sum, w) => sum + w, 0) + TAB_GAP * (TABS.length - 1)
}

// ── Cards ────────────────────────────────────────────────────────────────────

// A card whose `repos` meets the session's repos; one with none only from a session outside any repository.
export function isVisible(card: BoardCard, session: SessionRepos | null): boolean {
  if (session === null || !TABS.some(t => t.column === card.column)) return false
  const repos = card.repos ?? []
  return repos.length === 0 ? !session.in_repo : repos.some(r => session.repos.includes(r))
}

function dateOf(card: BoardCard): string {
  const date = card.frontmatter.date
  return typeof date === 'string' && date !== '' ? date : '9999-99-99'
}

// Priority high first, then the oldest date, then the slug.
export function sortCards(cards: readonly BoardCard[]): BoardCard[] {
  return [...cards].sort(
    (a, b) => b.priority - a.priority || dateOf(a).localeCompare(dateOf(b)) || a.slug.localeCompare(b.slug),
  )
}

export function tabCards(cards: readonly BoardCard[], session: SessionRepos | null, tab: number): BoardCard[] {
  const column = TABS[tab]?.column
  return sortCards(cards.filter(c => c.column === column && isVisible(c, session)))
}

export function tabCounts(cards: readonly BoardCard[], session: SessionRepos | null): number[] {
  return TABS.map((_, i) => tabCards(cards, session, i).length)
}

export function priorityMark(priority: number): { glyph: string; color: string } {
  return PRIORITY_MARKS[priority] ?? PRIORITY_MARKS[3] ?? { glyph: '=', color: 'yellow' }
}

export function tierMark(model: unknown): string {
  return typeof model === 'string' ? (TIERS[model] ?? '⬜') : '⬜'
}

// The meta row of a card: the priority mark (coloured), the rest (dim), then the slug (magenta) after a dim separator.
export function metaParts(card: BoardCard): { head: string; color: string; tail: string; slug: string } {
  const mark = priorityMark(card.priority)
  const model = typeof card.frontmatter.model === 'string' ? clean(card.frontmatter.model) : '?'
  const age = card.age_days === null ? '–' : `${card.age_days}d`
  return {
    head: `${mark.glyph} P${card.priority}`,
    color: mark.color,
    tail: ` · ${tierMark(card.frontmatter.model)} ${model} · ${age} · [${card.chantier_id ?? '----'}]`,
    slug: card.slug,
  }
}

// The `blocked_by` card while it is still open on the board (not in `4-done`).
export function blocker(card: BoardCard, cards: readonly BoardCard[]): string | null {
  const slug = card.frontmatter.blocked_by
  if (typeof slug !== 'string' || slug === '') return null
  const found = cards.find(c => c.slug === slug)
  return found !== undefined && found.column !== '4-done' ? slug : null
}

export function applicable(verb: 'select' | 'archive' | 'drop', column: string): boolean {
  if (verb === 'select') return column === '1-todo'
  if (verb === 'archive') return column === '3-in-progress' || column === '4-done'
  return TABS.some(t => t.column === column)
}

// ── Markdown ─────────────────────────────────────────────────────────────────

export type Span = { text: string; color?: string; bold?: boolean; dimColor?: boolean }
export type MdLine = Span[]
type Style = Omit<Span, 'text'>

const CODE_COLOR = 'blue'
const HEADING_COLOR = 'magenta'

const textOf = (spans: readonly Span[]) => spans.map(s => s.text).join('')

// `code`, **bold** and [label](url) of one line, the rest plain; `base` styles the plain text.
export function inlineSpans(text: string, base: Style = {}): Span[] {
  const spans: Span[] = []
  let at = 0
  for (const m of text.matchAll(/`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|\[([^\]]+)\]\([^)]*\)/g)) {
    if (m.index > at) spans.push({ ...base, text: text.slice(at, m.index) })
    if (m[1] !== undefined) spans.push({ ...base, text: m[1], color: CODE_COLOR })
    else if (m[2] !== undefined || m[3] !== undefined) spans.push({ ...base, text: m[2] ?? m[3] ?? '', bold: true })
    else spans.push({ ...base, text: m[4] ?? '' })
    at = m.index + m[0].length
  }
  if (at < text.length) spans.push({ ...base, text: text.slice(at) })
  return spans
}

const sameStyle = (a: Span, b: Span) => a.color === b.color && a.bold === b.bold && a.dimColor === b.dimColor

function merge(spans: Span[]): Span[] {
  const out: Span[] = []
  for (const s of spans) {
    const last = out[out.length - 1]
    if (last !== undefined && sameStyle(last, s)) last.text += s.text
    else out.push({ ...s })
  }
  return out
}

// Word wrap of styled text on display width; the first line starts with `lead`, the others are indented under it.
export function wrapSpans(spans: readonly Span[], width: number, lead: readonly Span[] = []): MdLine[] {
  const hang = displayWidth(textOf(lead))
  const room = Math.max(4, width - hang)
  const lines: Span[][] = [[]]
  let used = 0
  const line = () => lines[lines.length - 1] as Span[]
  const newLine = () => {
    const last = line()
    while (last.length > 0 && (last[last.length - 1] as Span).text.trim() === '') last.pop()
    lines.push([])
    used = 0
  }
  for (const span of spans) {
    for (const part of span.text.split(/( +)/)) {
      if (part === '') continue
      if (part.startsWith(' ')) {
        if (used > 0) {
          line().push({ ...span, text: part })
          used += part.length
        }
        continue
      }
      let rest = part
      if (used + displayWidth(rest) > room && used > 0) newLine()
      while (displayWidth(rest) > room) {
        let piece = ''
        for (const char of rest) {
          if (displayWidth(`${piece}${char}`) > room) break
          piece += char
        }
        line().push({ ...span, text: piece })
        rest = rest.slice(piece.length)
        newLine()
      }
      line().push({ ...span, text: rest })
      used += displayWidth(rest)
    }
  }
  const last = line()
  while (last.length > 0 && (last[last.length - 1] as Span).text.trim() === '') last.pop()
  return lines.map((l, i) => merge([...(i === 0 ? lead : hang === 0 ? [] : [{ text: ' '.repeat(hang) }]), ...l]))
}

// A table as aligned lines: each column as wide as its widest cell, the widest ones cut when the row overflows `width`.
export function tableLines(rows: readonly string[][], width: number): MdLine[] {
  const cells = rows.map(r => r.map(c => textOf(inlineSpans(c))))
  const cols = Math.max(0, ...cells.map(r => r.length))
  const wide = Array.from({ length: cols }, (_, i) => Math.max(...cells.map(r => displayWidth(r[i] ?? ''))))
  const gap = 2
  while (wide.reduce((n, w) => n + w, 0) + gap * Math.max(0, cols - 1) > width) {
    const widest = Math.max(...wide)
    if (widest <= 3) break
    wide[wide.indexOf(widest)] = widest - 1
  }
  return cells.map((r, row) => {
    const text = wide
      .map((w, i) => {
        const cell = fit(r[i] ?? '', w)
        return `${cell}${' '.repeat(Math.max(0, w - displayWidth(cell)))}`
      })
      .join(' '.repeat(gap))
      .trimEnd()
    return [row === 0 ? { text, dimColor: true } : { text }]
  })
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/

// A card or story body as styled lines of at most `width` cells: headings, bold, code, lists, tables and rules drawn,
// their markup gone. A blank line is an empty array.
export function renderMarkdown(source: string, width: number, options: { dropTitle?: boolean } = {}): MdLine[] {
  const out: MdLine[] = []
  let block: { lead: Span[]; style: Style; parts: string[]; isItem: boolean } | null = null
  let table: string[][] | null = null
  let inCode = false
  let hasTitle = options.dropTitle !== true
  const flush = () => {
    if (block !== null) out.push(...wrapSpans(inlineSpans(block.parts.join(' '), block.style), width, block.lead))
    block = null
    if (table !== null) out.push(...tableLines(table, width))
    table = null
  }
  const gap = () => {
    if (out.length > 0 && (out[out.length - 1] as MdLine).length > 0) out.push([])
  }
  const text = source.replace(/<!--[\s\S]*?-->/g, '')
  for (const raw of text.split('\n')) {
    const line = clean(raw.replace(/\t/g, '  '))
    if (/^\s*```/.test(line)) {
      flush()
      inCode = !inCode
      continue
    }
    if (inCode) {
      out.push([{ text: fit(`  ${line}`, width), color: CODE_COLOR }])
      continue
    }
    if (line.trimStart().startsWith('|')) {
      if (block !== null) flush()
      const cells = line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim())
      if (table === null) table = []
      if (!/^[\s|:-]+$/.test(line)) table.push(cells)
      continue
    }
    const heading = /^#{1,6}\s+(.*)$/.exec(line)
    const item = LIST_ITEM.exec(line)
    const openBlock = block as { parts: string[]; isItem: boolean } | null
    if (openBlock !== null && heading === null && item === null && !RULE.test(line) && line.trim() !== '' && (!openBlock.isItem || /^\s/.test(line))) {
      openBlock.parts.push(line.trim())
      continue
    }
    flush()
    if (heading !== null) {
      if (!hasTitle && /^#\s/.test(line)) {
        hasTitle = true
        continue
      }
      gap()
      out.push(...wrapSpans(inlineSpans(heading[1] ?? '', { color: HEADING_COLOR, bold: true }), width))
    } else if (RULE.test(line)) {
      gap()
      out.push([{ text: '─'.repeat(width), dimColor: true }])
    } else if (line.trim() === '') {
      gap()
    } else {
      if (item !== null) {
        const marker = /\d/.test(item[2] ?? '') ? (item[2] ?? '') : '•'
        const done = /^\[[xX]\]/.test(item[3] ?? '')
        block = { lead: [{ text: `${item[1] ?? ''}${marker} ` }], style: done ? { color: 'green' } : {}, parts: [item[3] ?? ''], isItem: true }
      } else block = { lead: [], style: {}, parts: [line.trim()], isItem: false }
    }
  }
  flush()
  while (out.length > 0 && (out[out.length - 1] as MdLine).length === 0) out.pop()
  return out
}

// The cards that fit in `rows`, the cursor's one always shown.
export function listWindow(heights: readonly number[], cursor: number, rows: number): [number, number] {
  if (heights.length === 0) return [0, 0]
  const at = Math.min(Math.max(cursor, 0), heights.length - 1)
  let start = at
  let used = heights[at] ?? 1
  while (start > 0 && used + (heights[start - 1] ?? 1) <= rows) used += heights[--start] ?? 1
  let end = at + 1
  while (end < heights.length && used + (heights[end] ?? 1) <= rows) used += heights[end++] ?? 1
  return [start, end]
}

// ── board_data.py output ─────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isCard(value: unknown): value is BoardCard {
  if (!isRecord(value)) return false
  const text = (k: string) => typeof value[k] === 'string'
  const textOrNull = (k: string) => value[k] === null || text(k)
  return (
    text('slug') && text('board') && text('chantier') && text('column') && text('path') && text('title') && text('body') &&
    textOrNull('chantier_id') && textOrNull('story') && (value.repos === null || Array.isArray(value.repos)) && isRecord(value.frontmatter) && typeof value.priority === 'number' &&
    (value.age_days === null || typeof value.age_days === 'number')
  )
}

function json(stdout: string): unknown {
  try {
    return JSON.parse(stdout)
  } catch {
    return undefined
  }
}

// `null` when the output is not a list: the caller tells a broken script from an empty board.
export function parseCards(stdout: string): BoardCard[] | null {
  const data = json(stdout)
  return Array.isArray(data) ? data.filter(isCard) : null
}

export function parseSession(stdout: string): SessionRepos | null {
  const data = json(stdout)
  if (!isRecord(data) || typeof data.in_repo !== 'boolean' || !Array.isArray(data.repos)) return null
  return { in_repo: data.in_repo, repos: data.repos.filter((r): r is string => typeof r === 'string') }
}

function isHit(value: unknown): value is SearchHit {
  return (
    isRecord(value) && typeof value.scope === 'string' && typeof value.path === 'string' &&
    typeof value.rel === 'string' && typeof value.line === 'number' && typeof value.text === 'string'
  )
}

export function parseSearch(stdout: string): SearchResult | null {
  const data = json(stdout)
  if (!isRecord(data) || typeof data.query !== 'string' || !isRecord(data.counts) || !Array.isArray(data.hits)) return null
  const counts: Record<string, number> = {}
  for (const [k, v] of Object.entries(data.counts)) if (typeof v === 'number') counts[k] = v
  return { query: data.query, counts, hits: data.hits.filter(isHit), truncated: data.truncated === true }
}

// ── Navigation ───────────────────────────────────────────────────────────────

export type Layout = { columns: number; rows: number }
export type Verb = 'select' | 'archive' | 'drop'

export type Intent =
  | { t: 'tab'; index: number }
  | { t: 'tabs'; d: number }
  | { t: 'cursor'; d: number }
  | { t: 'open'; key?: string }
  | { t: 'back' }
  | { t: 'story' }
  | { t: 'notice' }
  | { t: 'scroll'; d: number }
  | { t: 'refresh' }
  | { t: 'find' }
  | { t: 'editor' }
  | { t: 'act'; verb: 'select' | 'archive' }
  | { t: 'drop'; phase: 'start' | 'cancel' }
  | { t: 'scope'; name: string }
  | { t: 'hit'; index: number }

export type Effect =
  | { t: 'refresh' }
  | { t: 'story'; path: string }
  | { t: 'editor'; path: string; line: number }
  | { t: 'act'; verb: 'select' | 'archive'; slug: string }
  | { t: 'search'; query: string; scopes: string[] }

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n))

// A card is told apart by board, chantier and slug: the slug alone can repeat across them.
export function cardKey(card: BoardCard): string {
  return `${card.board}|${card.chantier}|${card.slug}`
}

export function openCard(s: BoardState): BoardCard | undefined {
  return s.cards.find(c => cardKey(c) === s.cardKey)
}

// ── Card and story screens ───────────────────────────────────────────────────

// Rows an Input may take, the frame it draws included: the layout holds them back rather than push the footer off.
export const INPUT_ROWS = 3

// Greedy packing of items of the given widths into rows of at most `width` cells, `gap` cells apart.
export function packRows(widths: readonly number[], width: number, gap: number): number[][] {
  const rows: number[][] = [[]]
  let used = 0
  widths.forEach((w, i) => {
    const row = rows[rows.length - 1] as number[]
    if (row.length > 0 && used + gap + w > width) {
      rows.push([i])
      used = w
    } else {
      row.push(i)
      used += (row.length > 1 ? gap : 0) + w
    }
  })
  return rows
}

function pack<T>(items: readonly T[], widthOf: (item: T) => number, width: number, gap: number): T[][] {
  return items.length === 0 ? [] : packRows(items.map(widthOf), width, gap).map(row => row.map(i => items[i] as T))
}

export type Seg = { text: string; color?: string; bold?: boolean; dimColor?: boolean; press?: Intent }

const DATE_WORD: Record<string, string> = {
  '0-ideas': 'surfaced', '1-todo': 'scoped', '2-selected-for-dev': 'selected', '3-in-progress': 'in progress since',
}

export const chantierName = (card: BoardCard) => card.chantier.replace(/^CHANTIER\d+-/, '')

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string' && v !== '') : []
}

// The badge line of an open card: column, priority, tier, effort, chantier, date, repos, serves, requirements, blocker.
export function cardSegments(card: BoardCard, cards: readonly BoardCard[]): Seg[] {
  const fm = card.frontmatter
  const mark = priorityMark(card.priority)
  const model = typeof fm.model === 'string' ? fm.model : '?'
  const effort = typeof fm.effort === 'string' && fm.effort !== '' ? fm.effort : null
  const chantier = `[${card.chantier_id ?? '----'}]`
  const date = typeof fm.date === 'string' && fm.date !== '' ? fm.date : null
  const serve = typeof fm.serves === 'string' && fm.serves !== '' ? fm.serves : null
  const target = serve === null ? undefined : cards.find(c => c.slug === serve)
  const blocked = blocker(card, cards)
  const segs: Seg[] = [
    { text: card.column },
    { text: `${mark.glyph} P${card.priority}`, color: mark.color, bold: true },
    { text: `${tierMark(fm.model)} ${model}` },
    ...(effort === null ? [] : [{ text: `effort ${effort}` }]),
    card.story === null ? { text: chantier } : { text: chantier, press: { t: 'story' } as Intent },
    ...(date === null ? [] : [{ text: `${DATE_WORD[card.column] ?? 'on'} ${date}`, dimColor: true }]),
    ...strings(card.repos).map(r => ({ text: r })),
    ...(serve === null ? [] : [target === undefined ? { text: `serves → ${serve}` } : { text: `serves → ${serve}`, press: { t: 'open', key: cardKey(target) } as Intent }]),
    ...strings(fm.requirements).map(r => ({ text: r })),
    ...(blocked === null ? [] : [{ text: `⊘ blocked by ${blocked}`, color: 'red' }]),
  ]
  return segs.map(seg => ({ ...seg, text: clean(seg.text) }))
}

export type ActionKey = 'edit' | 'select' | 'archive' | 'drop'
export type Action = { key: ActionKey; label: string; color: string; state: 'main' | 'normal' | 'off'; intent: Intent }

const MAIN_ACTION: Record<string, ActionKey> = { '0-ideas': 'edit', '1-todo': 'select', '2-selected-for-dev': 'edit', '3-in-progress': 'archive' }

// The action row: the editor and the verbs, the column's main one marked, the ones that do not apply off.
export function cardActions(card: BoardCard | undefined, isStory: boolean): Action[] {
  const edit: Action = { key: 'edit', label: '✎ open in editor', color: 'yellow', state: !isStory && card !== undefined && MAIN_ACTION[card.column] === 'edit' ? 'main' : 'normal', intent: { t: 'editor' } }
  if (isStory || card === undefined) return [edit]
  const verb = (key: Exclude<ActionKey, 'edit'>, label: string, color: string, intent: Intent): Action => ({
    key, label, color, intent,
    state: !applicable(key, card.column) ? 'off' : MAIN_ACTION[card.column] === key ? 'main' : 'normal',
  })
  return [
    edit,
    verb('select', '↑ select', 'green', { t: 'act', verb: 'select' }),
    verb('archive', '▣ archive', 'blue', { t: 'act', verb: 'archive' }),
    verb('drop', '✕ drop', 'red', { t: 'drop', phase: 'start' }),
  ]
}

const BUTTON_CHROME = 4 // `[ ` and ` ]` around a Button label
const HOTKEY_CHROME = 3 // `x: ` before a plain Button's label

export const hotkeyWidth = (k: Hotkey) => displayWidth(k.label) + HOTKEY_CHROME
export const actionWidth = (a: Action) => displayWidth(a.label) + BUTTON_CHROME

// A story's title: the first `# ` heading of its text, else a name for the chantier.
export function storyTitle(text: string | null, card: BoardCard): string {
  const found = /^#\s+(.+)$/m.exec(text ?? '')
  return clean(found?.[1] ?? `Story · ${chantierName(card)}`)
}

// The ` · ` between two badges, as the list's meta row joins its parts.
export const SEG_GAP = 3

export type CardParts = {
  title: string[]
  sub: string
  segRows: Seg[][]
  actionRows: Action[][]
  footerRows: Hotkey[][]
  lines: MdLine[]
  // Every row the screen draws but the body, and the rows the body gets: together the pane's rows.
  chrome: number
  bodyRows: number
}

// What the card and story screens draw, row by row: the one place that counts the rows, for the draw and the scroll clamp.
export function cardParts(s: BoardState, card: BoardCard, layout: Layout): CardParts {
  const width = layout.columns - 1
  const isStory = s.screen === 'story'
  const title = isStory ? [storyTitle(s.storyText, card)] : wrapText(clean(card.title), width)
  const sub = isStory ? `_KANBAN/${card.chantier}/story.md` : card.slug
  const segs: Seg[] = isStory ? [{ text: `[${card.chantier_id ?? '----'}]` }] : cardSegments(card, s.cards)
  const segRows = pack(segs.map(g => ({ ...g, text: fit(g.text, width) })), g => displayWidth(g.text), width, SEG_GAP)
  const actionRows = pack(cardActions(card, isStory), actionWidth, width, 1)
  const footerRows = s.isDropping ? [] : pack(hotkeys(s.screen, 'edit', s.notice !== null), hotkeyWidth, width, 2)
  const source = isStory ? (s.storyText ?? 'Reading the story…') : card.body
  const lines = renderMarkdown(source, width, { dropTitle: true })
  const notice = noticeLines(s)
  const footer = s.isDropping ? 1 : footerRows.length
  // title, rule, sub, badges, rule, [body], rule, navigation, actions, notice, drop field, footer.
  const chrome = title.length + 1 + 1 + segRows.length + 1 + 1 + 1 + actionRows.length + notice + (s.isDropping ? INPUT_ROWS : 0) + footer
  return { title, sub, segRows, actionRows, footerRows, lines, chrome, bodyRows: Math.max(3, layout.rows - chrome) }
}

// The notice screen (hotkey n): the whole text wrapped to the width — one place counting its rows, for the draw and the scroll clamp.
export function noticeParts(s: BoardState, layout: Layout): { lines: string[]; footerRows: Hotkey[][]; chrome: number; bodyRows: number } {
  const width = layout.columns - 1
  const lines = s.notice === null ? [] : s.notice.text.split('\n').flatMap(l => wrapText(l, width))
  const footerRows = pack(hotkeys('notice'), hotkeyWidth, width, 2)
  // title, rule, rule(range), navigation, footer.
  const chrome = 4 + footerRows.length
  return { lines, footerRows, chrome, bodyRows: Math.max(3, layout.rows - chrome) }
}

function scrollMax(s: BoardState, layout: Layout): number {
  if (s.screen === 'notice') {
    const parts = noticeParts(s, layout)
    return Math.max(0, parts.lines.length - parts.bodyRows)
  }
  const card = openCard(s)
  if (card === undefined) return 0
  const parts = cardParts(s, card, layout)
  return Math.max(0, parts.lines.length - parts.bodyRows)
}

export const noticeLines = (s: BoardState) => (s.notice === null ? 0 : trimNotice(s.notice.text).split('\n').length)

const note = (s: BoardState, kind: 'info' | 'error', text: string): BoardState => ({ ...s, notice: { kind, text } })

export function reduce(s: BoardState, intent: Intent, layout: Layout): { state: BoardState; effect?: Effect } {
  // The drop field belongs to the card it was started on: any other intent leaves it first.
  if (s.isDropping && intent.t !== 'drop') s = { ...s, isDropping: false }
  const here = tabCards(s.cards, s.session, s.tab)
  const card = s.screen === 'list' ? here[s.cursor] : openCard(s)
  switch (intent.t) {
    case 'tab':
      return { state: { ...s, screen: 'list', tab: clamp(intent.index, 0, TABS.length - 1), cursor: 0, scroll: 0, notice: null } }
    case 'tabs':
      return s.screen === 'list' ? reduce(s, { t: 'tab', index: s.tab + intent.d }, layout) : { state: s }
    case 'cursor': {
      const size = s.screen === 'search' ? (s.search?.hits.length ?? 0) : here.length
      return s.screen === 'list' || s.screen === 'search'
        ? { state: { ...s, cursor: clamp(s.cursor + intent.d, 0, Math.max(0, size - 1)) } }
        : { state: s }
    }
    case 'open': {
      const key = intent.key ?? (card === undefined ? undefined : cardKey(card))
      return key === undefined
        ? { state: s }
        : { state: { ...s, screen: 'card', cardKey: key, scroll: 0, notice: null, isDropping: false } }
    }
    case 'back': {
      if (s.screen === 'search') {
        const { screen, cursor } = s.searchFrom
        const isOpen = (screen === 'card' || screen === 'story') && openCard(s) !== undefined
        return { state: { ...s, screen: isOpen || screen === 'list' ? screen : 'list', cursor, scroll: 0, notice: null } }
      }
      if (s.screen === 'notice') {
        // The card left stays addressed: back returns to it, else to the list. The notice stays — it is what the screen shows.
        return { state: { ...s, screen: openCard(s) !== undefined ? 'card' : 'list', scroll: 0 } }
      }
      if (s.screen === 'story') return { state: { ...s, screen: 'card', scroll: 0, notice: null } }
      const current = s.screen === 'card' ? openCard(s) : undefined
      const tab = Math.max(0, TABS.findIndex(t => t.column === current?.column))
      const cursor = s.screen === 'card' ? Math.max(0, tabCards(s.cards, s.session, tab).findIndex(c => cardKey(c) === s.cardKey)) : s.cursor
      return { state: { ...s, screen: 'list', tab: s.screen === 'card' ? tab : s.tab, cursor, scroll: 0, notice: null, isDropping: false } }
    }
    case 'find':
      return {
        state: {
          ...s,
          screen: 'search',
          searchFrom: s.screen === 'search' ? s.searchFrom : { screen: s.screen, cursor: s.cursor },
          search: s.search ?? { query: '', counts: {}, hits: [], truncated: false },
          searchMode: 'edit',
          cursor: 0,
          scroll: 0,
          notice: null,
          isDropping: false,
        },
      }
    case 'story': {
      if (card === undefined) return { state: s }
      if (card.story === null) return { state: note({ ...s, cardKey: cardKey(card) }, 'error', 'This chantier has no story.md.') }
      return {
        state: { ...s, screen: 'story', cardKey: cardKey(card), scroll: 0, storyText: null, notice: null },
        effect: { t: 'story', path: card.story },
      }
    }
    case 'notice': {
      if (s.notice === null) return { state: s }
      if (s.screen === 'notice') return reduce(s, { t: 'back' }, layout)
      return { state: { ...s, screen: 'notice', scroll: 0 } }
    }
    case 'scroll':
      return s.screen === 'card' || s.screen === 'story' || s.screen === 'notice'
        ? { state: { ...s, scroll: clamp(s.scroll + intent.d, 0, scrollMax(s, layout)) } }
        : { state: s }
    case 'refresh':
      return {
        state: { ...s, notice: null },
        effect: s.screen === 'search' && s.search !== null ? { t: 'search', query: s.search.query, scopes: s.scopes } : { t: 'refresh' },
      }
    case 'editor': {
      if (s.screen === 'search') return reduce(s, { t: 'hit', index: s.cursor }, layout)
      const path = s.screen === 'story' ? card?.story : card?.path
      return path === undefined || path === null ? { state: s } : { state: s, effect: { t: 'editor', path, line: 1 } }
    }
    case 'act':
      if (s.screen !== 'card' || card === undefined) return { state: s }
      return applicable(intent.verb, card.column)
        ? { state: s, effect: { t: 'act', verb: intent.verb, slug: card.slug } }
        : { state: note(s, 'error', `${intent.verb} does not apply to a card in ${card.column}.`) }
    case 'drop':
      if (intent.phase === 'cancel') return { state: { ...s, isDropping: false } }
      return card !== undefined && applicable('drop', card.column)
        ? { state: { ...s, isDropping: true, notice: null } }
        : { state: note(s, 'error', 'drop does not apply to this card.') }
    case 'scope': {
      const on = s.scopes.includes(intent.name)
      const scopes = on ? s.scopes.filter(n => n !== intent.name) : SCOPES.filter(n => n === intent.name || s.scopes.includes(n))
      if (scopes.length === 0) return { state: s }
      // Editing: the typed text is not a search yet, the toggle only picks scopes. A search never runs on an empty query.
      if (s.searchMode === 'edit' || s.search === null || s.search.query.trim() === '') return { state: { ...s, scopes } }
      return { state: { ...s, scopes }, effect: { t: 'search', query: s.search.query, scopes } }
    }
    case 'hit': {
      const hit = s.search?.hits[intent.index]
      return hit === undefined ? { state: s } : { state: { ...s, cursor: intent.index }, effect: { t: 'editor', path: hit.path, line: hit.line } }
    }
  }
}

// ── Engine side ──────────────────────────────────────────────────────────────

// The last size the pane was drawn at, read by the scroll clamp.
const layout: Layout = { columns: 70, rows: 24 }

async function script($: EngineInterface, relative: string, args: string[]) {
  const root = await $.session.root()
  return $.process.run(['python3', `${$.plugin.root}/${relative}`, ...args], { cwd: root })
}

// One cleaned line per line of `text`: a script's output is text the pane does not author, and the newlines are the structure of a refusal.
function sanitizeNotice(text: string): string {
  return text.split('\n').map(clean).join('\n')
}

// The collapsed band keeps the first MAX_NOTICE_LINES lines; the expanded screen (hotkey n) shows the whole text.
export function trimNotice(text: string): string {
  const lines = text.split('\n')
  if (lines.length <= MAX_NOTICE_LINES) return text
  return [...lines.slice(0, MAX_NOTICE_LINES), `… (+${lines.length - MAX_NOTICE_LINES} lines)`].join('\n')
}

async function set($: EngineInterface, change: (s: BoardState) => BoardState) {
  await update($, board, change)
}

// Reads the cards and the session's repos again.
async function load($: EngineInterface) {
  try {
    const cards = await script($, DATA, ['cards'])
    const repos = await script($, DATA, ['session-repos', await $.session.root()])
    const parsed = cards.exitCode === 0 ? parseCards(cards.stdout) : null
    const session = repos.exitCode === 0 ? parseSession(repos.stdout) : null
    if (parsed === null || session === null) {
      const why = sanitizeNotice(cards.exitCode === 0 && repos.exitCode === 0 ? 'unreadable output' : `${cards.stderr}${repos.stderr}`)
      await set($, s => ({ ...s, isLoaded: true, notice: { kind: 'error', text: `Could not read the board: ${why}` } }))
      return
    }
    await set($, s => ({ ...s, cards: parsed, session, isLoaded: true }))
  } catch (error) {
    await set($, s => ({ ...s, isLoaded: true, notice: { kind: 'error', text: `Could not read the board: ${sanitizeNotice(String(error))}` } }))
  }
}

// The session's repos filter the search only from inside a repository; none declared, nothing to filter on.
export function searchArgs(session: SessionRepos | null, scopes: string[], query: string): string[] {
  const repos = session?.in_repo === true ? session.repos : []
  return ['search', '--scope', scopes.join(','), ...(repos.length === 0 ? [] : ['--repos', repos.join(',')]), '--', query]
}

// Each search takes a number; only the latest-started one may land.
let latestRun = 0

// `isOpening`: the screen is not `search` yet, `/board search` opens straight onto the results.
async function runSearch($: EngineInterface, query: string, scopes: string[], isOpening = false) {
  const run = ++latestRun
  try {
    const done = await script($, DATA, searchArgs((await read($, board)).session, scopes, query))
    const result = done.exitCode === 0 ? parseSearch(done.stdout) : null
    if (result === null) throw new Error(done.stderr || 'unreadable output')
    if (run !== latestRun) return
    await set($, s => s.screen === 'search' || isOpening
      ? { ...s, screen: 'search', searchMode: 'results', search: result, scopes, cursor: 0, scroll: 0, notice: null }
      : { ...s, scopes })
    await focusRing($, await read($, board))
  } catch (error) {
    if (run !== latestRun) return
    await set($, s => ({ ...s, notice: { kind: 'error', text: `Search failed: ${sanitizeNotice(String(error))}` } }))
  }
}

function quote(path: string): string {
  return /^[\w@%+=:,./-]+$/.test(path) ? path : `'${path.replace(/'/g, `'\\''`)}'`
}

const KANBAN_WORKSPACE = 'kanban'
const CMUX_ENV = { CMUX_QUIET: '1' }

// The ref of the workspace titled exactly `kanban` in `cmux workspace list --json` output; null when there is none or
// the output is not that JSON.
export function kanbanWorkspaceRef(stdout: string): string | null {
  const data = json(stdout)
  if (!isRecord(data) || !Array.isArray(data.workspaces)) return null
  const found = data.workspaces.find((w: unknown) => isRecord(w) && w.custom_title === KANBAN_WORKSPACE && typeof w.ref === 'string' && w.ref !== '')
  return isRecord(found) ? String(found.ref) : null
}

// Runs a cmux command; its stdout, or null on a non-zero exit or a throw.
async function cmux($: EngineInterface, argv: string[]): Promise<string | null> {
  try {
    const done = await $.process.run(['cmux', ...argv], { env: CMUX_ENV })
    return done.exitCode === 0 ? done.stdout : null
  } catch {
    return null
  }
}

// Opens `path` in the dedicated `kanban` cmux workspace, made on first use and reused after: a tab in the session's own
// workspace could shrink Claude's terminal. False when any step fails.
async function openInKanbanWorkspace($: EngineInterface, path: string): Promise<boolean> {
  // `undefined` when the list itself failed, `null` when it holds no kanban workspace.
  const find = async () => {
    const listed = await cmux($, ['workspace', 'list', '--json'])
    return listed === null ? undefined : kanbanWorkspaceRef(listed)
  }
  let ref = await find()
  if (ref === null) {
    const dir = path.slice(0, path.lastIndexOf('/')) || '/'
    if ((await cmux($, ['new-workspace', '--name', KANBAN_WORKSPACE, '--cwd', dir, '--focus', 'false'])) === null) return false
    ref = await find()
  }
  return typeof ref === 'string' && (await cmux($, ['open', path, '--workspace', ref, '--focus', 'true'])) !== null
}

// Opens a file: in the `kanban` cmux workspace when the session runs inside cmux (no line), else VS Code at the line,
// else the system's text editor; only when all fail, the command line.
async function openFile($: EngineInterface, path: string, line: number) {
  const inCmux = await $.env.get('CMUX_SURFACE_ID').then(v => (v ?? '') !== '', () => false)
  if (inCmux && (await openInKanbanWorkspace($, path))) return set($, s => note(s, 'info', `Opened ${clean(path)}`))
  const attempts = [['code', '-g', `${path}:${line}`], ['open', '-t', path]]
  for (const argv of attempts) {
    try {
      const done = await $.process.run(argv)
      if (done.exitCode === 0) return set($, s => note(s, 'info', `Opened ${clean(path)}${argv[0] === 'code' ? `:${line}` : ''}`))
    } catch {
      // the next opener
    }
  }
  return set($, s => note(s, 'info', `No editor could be started. Open it with: $EDITOR +${line} ${quote(path)}`))
}

// How card_ops is told which card: `<id>/<slug>` when the chantier has an id, since the slug alone can repeat across chantiers.
const cardRef = (card: BoardCard) => (card.chantier_id === null ? card.slug : `${card.chantier_id}/${card.slug}`)

// The line a closure fills into the prompt box with: what was closed and the follow-up the model runs. The closing
// sequence's own output is not pasted — the model regenerates it.
const PAST_TENSE: Record<'archive' | 'drop', string> = { archive: 'archived', drop: 'dropped' }
export function buildFollowUpLine(ref: string, verb: 'archive' | 'drop'): string {
  return `${ref} ${PAST_TENSE[verb]} from the board pane — run the closing sequence (obsolescence check, next-card proposal, session reco); the pane did not capture its output.`
}

// The user is the notification channel after a closure from the pane: the follow-up line goes into the prompt box
// by append, and only the user's sending it starts a turn — never a submit, never a session append. A fill the box
// refuses (a dialog open, no box) changes nothing: the notice alone carries the result.
async function followUp($: EngineInterface, ref: string, verb: 'archive' | 'drop') {
  // The leading newline is constant on purpose — reading the box to trim it would reintroduce the race.
  const text = `\n${buildFollowUpLine(ref, verb)}`
  try {
    await $.prompt.fill({ text, mode: 'append' })
  } catch {
    // the notice alone carries the result
  }
}

// What the card ops script prints, or its refusal, in the pane; the board is read again either way. `ref` is what
// card_ops is told: `<id>/<slug>` when the chantier has an id, else the bare slug.
async function runOp($: EngineInterface, verb: Verb, ref: string, reason?: string) {
  try {
    const done = await script($, CARD_OPS, ['--', verb, ref, ...(reason === undefined ? [] : [reason])])
    const ok = done.exitCode === 0
    const text = sanitizeNotice((ok ? done.stdout : `${done.stderr}${done.stdout}`).trim() || (ok ? `${verb} done.` : `${verb} refused.`))
    await set($, s => ({ ...s, notice: { kind: ok ? 'info' : 'error', text }, isDropping: ok ? false : s.isDropping }))
    await load($)
    // A selected card stays open in its new column; an archived or dropped one is gone from the board.
    if (ok && verb !== 'select') {
      await set($, s => ({ ...s, screen: 'list', cardKey: null, cursor: 0 }))
      await followUp($, ref, verb)
    }
  } catch (error) {
    await set($, s => ({ ...s, notice: { kind: 'error', text: `${verb} failed: ${sanitizeNotice(String(error))}` } }))
  }
}

async function perform($: EngineInterface, effect: Effect) {
  switch (effect.t) {
    case 'refresh':
      return load($)
    case 'search':
      return runSearch($, effect.query, effect.scopes)
    case 'act': {
      const card = openCard(await read($, board))
      return runOp($, effect.verb, card?.slug === effect.slug ? cardRef(card) : effect.slug)
    }
    case 'editor':
      return openFile($, effect.path, effect.line)
    case 'story': {
      let done
      try {
        done = await $.process.run(['cat', effect.path])
      } catch (error) {
        const text = `Could not read the story: ${trimNotice(String(error))}`
        return set($, s => ({ ...s, screen: s.screen === 'story' ? 'card' : s.screen, storyText: null, notice: { kind: 'error', text } }))
      }
      const text = done.exitCode === 0 ? done.stdout : `Could not read the story: ${trimNotice(done.stderr)}`
      return set($, s => ({ ...s, storyText: text }))
    }
  }
}

// Keys of the focus ring's targets: a card's open Button, the drop reason field.
export const cardFocusKey = (card: BoardCard) => `card:${cardKey(card)}`
const DROP_KEY = 'drop-reason'
const SEARCH_KEY = 'search-input'
export const hitKey = (index: number) => `hit-${index}`

// The element the focus ring goes to after a move: the drop field, else the selected card of the list.
export function focusKey(state: BoardState): string | undefined {
  if (state.isDropping) return DROP_KEY
  if (state.screen === 'search') return state.searchMode === 'edit' ? SEARCH_KEY : (state.search?.hits.length ?? 0) > 0 ? hitKey(Math.min(state.cursor, (state.search?.hits.length ?? 1) - 1)) : undefined
  if (state.screen !== 'list') return undefined
  const here = tabCards(state.cards, state.session, state.tab)
  const selected = here[Math.min(state.cursor, here.length - 1)]
  return selected === undefined ? undefined : cardFocusKey(selected)
}

// A `{ deny }` (the pane does not hold the keys) or a failure leaves the ring where it is: no notice.
async function focusRing($: EngineInterface, state: BoardState) {
  const key = focusKey(state)
  if (key === undefined) return
  try {
    await $.ui.focus({ requestId: PANE, key })
  } catch {
    // the ring stays where it is
  }
}

export const REFOCUS: readonly Intent['t'][] = ['tab', 'tabs', 'cursor', 'back', 'refresh', 'find']

export async function dispatch($: EngineInterface, intent: Intent) {
  let effect: Effect | undefined
  // The updater may run again on a version miss: the effect kept is the one of the run that wrote.
  const state = await update($, board, s => {
    const reduced = reduce(s, intent, layout)
    effect = reduced.effect
    return reduced.state
  })
  if (effect !== undefined) await perform($, effect)
  if (REFOCUS.includes(intent.t) || (intent.t === 'drop' && intent.phase === 'start')) await focusRing($, effect === undefined ? state : await read($, board))
}

async function drop($: EngineInterface, reason: string) {
  const state = await read($, board)
  const card = openCard(state)
  if (card === undefined || !state.isDropping) return
  if (reason.trim() === '') return set($, s => note(s, 'error', 'drop needs a reason.'))
  return runOp($, 'drop', cardRef(card), reason.trim())
}

async function open($: EngineInterface, args: string) {
  const search = /^search\s+(\S.*)$/.exec(args)
  if (args !== '' && search === null) return 'Usage: /board  or  /board search <text>'
  await set($, () => ({ ...initialState }))
  await load($)
  if (search?.[1] !== undefined) await runSearch($, search[1].trim(), [...SCOPES], true)
  const opened = await $.ui.open({ id: PANE, title: 'Board', focus: true })
  return opened.isPlaced ? 'Board pane opened.' : 'Board pane waits: widen the terminal, or press ctrl+x tab.'
}

// ── Drawing ──────────────────────────────────────────────────────────────────

export type Hotkey = { letter: string; label: string; intent: Intent }

// The letter keys of a screen: the footer draws them as Buttons, so the legend is also the control.
export function hotkeys(screen: BoardState['screen'], mode: BoardState['searchMode'] = 'edit', hasNotice = false): Hotkey[] {
  const refresh: Hotkey = { letter: 'r', label: 'refresh', intent: { t: 'refresh' } }
  const find: Hotkey = { letter: 'f', label: 'search', intent: { t: 'find' } }
  // The full notice screen holds one text: scroll it, close it.
  if (screen === 'notice') {
    return [
      { letter: 'j', label: 'down', intent: { t: 'scroll', d: 1 } },
      { letter: 'k', label: 'up', intent: { t: 'scroll', d: -1 } },
      { letter: 'n', label: 'close', intent: { t: 'notice' } },
    ]
  }
  // The search screen holds a field while editing: letters type there, so it has no letter key. Its results have them.
  if (screen === 'search') {
    return mode === 'edit'
      ? []
      : [
          { letter: 'h', label: 'back', intent: { t: 'back' } },
          { letter: 'j', label: 'next', intent: { t: 'cursor', d: 1 } },
          { letter: 'k', label: 'prev', intent: { t: 'cursor', d: -1 } },
          { letter: 'f', label: 'edit query', intent: { t: 'find' } },
          { letter: 'r', label: 'rerun', intent: { t: 'refresh' } },
        ]
  }
  // The letter n expands a notice into its full screen; offered where a closure lands (the list, the card).
  const notice: Hotkey[] = hasNotice && (screen === 'list' || screen === 'card') ? [{ letter: 'n', label: 'notice', intent: { t: 'notice' } }] : []
  if (screen === 'list') {
    return [
      { letter: 'h', label: '◀ state', intent: { t: 'tabs', d: -1 } },
      { letter: 'l', label: 'state ▶', intent: { t: 'tabs', d: 1 } },
      { letter: 'j', label: 'next', intent: { t: 'cursor', d: 1 } },
      { letter: 'k', label: 'prev', intent: { t: 'cursor', d: -1 } },
      { letter: 's', label: 'story', intent: { t: 'story' } },
      ...notice,
      find,
      refresh,
    ]
  }
  return [
    { letter: 'j', label: 'down', intent: { t: 'scroll', d: 1 } },
    { letter: 'k', label: 'up', intent: { t: 'scroll', d: -1 } },
    { letter: 'h', label: 'back', intent: { t: 'back' } },
    { letter: 'e', label: 'editor', intent: { t: 'editor' } },
    ...(screen === 'card' ? [{ letter: 's', label: 'story', intent: { t: 'story' } } as Hotkey] : []),
    ...notice,
    find,
    refresh,
  ]
}

// A horizontal rule `width` cells wide, a label set into it when there is one.
export function ruleLine(width: number, label = ''): string {
  if (label === '') return '─'.repeat(width)
  const tag = `── ${label} `
  return `${tag}${'─'.repeat(Math.max(0, width - displayWidth(tag)))}`
}

export type ListParts = {
  here: BoardCard[]
  heights: number[]
  from: number
  to: number
  footerRows: Hotkey[][]
  hasCount: boolean
  // Blank rows that keep the footer on the pane's last row.
  pad: number
}

// The list screen's rows: title, tabs, rule, the cards that fit, a count row when some do not, notice, footer.
export function listParts(s: BoardState, layout: Layout): ListParts {
  const here = tabCards(s.cards, s.session, s.tab)
  const inner = layout.columns - 4
  const heights = here.map(c => titleLines(c.title, inner).length + 1 + (blocker(c, s.cards) === null ? 0 : 1) + 2)
  const footerRows = pack(hotkeys('list', 'edit', s.notice !== null), hotkeyWidth, layout.columns - 1, 2)
  const tabs = tabRows(tabCounts(s.cards, s.session), layout.columns).length
  // title, tabs, rule, then the cards, notice and footer.
  const room = Math.max(3, layout.rows - (2 + tabs + noticeLines(s) + footerRows.length))
  const total = heights.reduce((n, h) => n + h, 0)
  const hasCount = total > room
  const [from, to] = hasCount ? listWindow(heights, s.cursor, room - 1) : [0, here.length]
  const used = heights.slice(from, to).reduce((n, h) => n + h, 0) + (here.length === 0 ? 1 : 0) + (hasCount ? 1 : 0)
  return { here, heights, from, to, footerRows, hasCount, pad: Math.max(0, room - used) }
}

export const scopeLabel = (name: string, isOn: boolean, count: number) => `${isOn ? '☑' : '☐'} ${name} ${count}`

export type SearchParts = { scopeRows: string[][]; footerRows: Hotkey[][]; room: number }

// The search screen's rows: navigation, then the field (editing) or the footer (results), scopes, summary, the result
// rows that fit, notice, truncation note.
export function searchParts(s: BoardState, layout: Layout): SearchParts {
  const labels = SCOPES.map(name => scopeLabel(name, s.scopes.includes(name), s.search?.counts[name] ?? 0))
  const scopeRows = pack(labels, l => displayWidth(l) + BUTTON_CHROME - 2, layout.columns - 1, 1)
  const footerRows = s.searchMode === 'edit' ? [] : pack(hotkeys('search', 'results'), hotkeyWidth, layout.columns - 1, 2)
  const fixed = 1 + (s.searchMode === 'edit' ? INPUT_ROWS : footerRows.length) + scopeRows.length + 1 + noticeLines(s) + (s.search?.truncated === true ? 1 : 0)
  return { scopeRows, footerRows, room: Math.max(3, layout.rows - fixed) }
}

export type SearchRow = { key: string; hit?: SearchHit; index?: number; file?: string; count?: number }

// The hits as rows: a file header before the first hit of each file.
export function searchRows(hits: readonly SearchHit[]): SearchRow[] {
  const rows: SearchRow[] = []
  hits.forEach((hit, index) => {
    if (hit.path !== hits[index - 1]?.path) rows.push({ key: `file-${index}`, file: hit.rel, count: hits.filter(h => h.path === hit.path).length })
    rows.push({ key: hitKey(index), hit, index })
  })
  return rows
}

// The rows of `room` that follow the cursor hit: the cursor's row always shown, its file header kept above a window that
// starts mid-file, and a count row (`a–b / n` hits) in the room when the hits do not all fit.
export function searchWindow(rows: readonly SearchRow[], cursor: number, room: number): { shown: SearchRow[]; count: string } {
  const hits = rows.filter(r => r.hit !== undefined).length
  const at = Math.max(0, rows.findIndex(r => r.index === cursor))
  const ones = rows.map(() => 1)
  if (rows.length <= room) return { shown: [...rows], count: '' }
  let [from, to] = listWindow(ones, at, room - 1)
  let shown = rows.slice(from, to)
  if (rows[from]?.hit !== undefined) {
    ;[from, to] = listWindow(ones, at, room - 2)
    shown = rows.slice(from, to)
    if (rows[from]?.hit !== undefined) {
      const header = rows.slice(0, from).reverse().find(r => r.file !== undefined)
      if (header !== undefined) shown = [header, ...shown]
    }
  }
  const indexes = shown.flatMap(r => (r.index === undefined ? [] : [r.index]))
  return { shown, count: `${(indexes[0] ?? 0) + 1}–${(indexes[indexes.length - 1] ?? 0) + 1} / ${hits}` }
}

const style = (s: Span) => ({
  ...(s.color === undefined ? {} : { color: s.color }),
  ...(s.bold === true ? { bold: true } : {}),
  ...(s.dimColor === true ? { dimColor: true } : {}),
})

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'board', description: 'Show the kanban board of this session in a pane', argumentHint: '[search <text>]' })
    return next(e)
  })

  on('command.run', { command: 'board' }, async ($, e) => ({ text: await open($, e.args.trim()) }))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const state = await read($, board)
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const Input = 'Input' in els ? els.Input : undefined
    layout.columns = Math.max(20, e.props.bodyColumns)
    layout.rows = e.props.scroll.bodyRows > 0 ? e.props.scroll.bodyRows : (e.viewport?.rows ?? 24)
    const width = layout.columns - 1
    const go = (intent: Intent) => () => void dispatch($, intent)

    // A Button in a keyed Box, so its `hover` lights it: magenta unless it names a colour, never the engine's black.
    const press = (id: string, label: string, onPress: () => void, o: { color?: string; hotkey?: string; plain?: boolean; dim?: boolean; primary?: boolean } = {}) => (
      <Box key={`box:${id}`}>
        <Button
          key={id}
          label={label}
          onPress={onPress}
          {...(o.plain === true ? { plain: true as const } : {})}
          {...(o.hotkey === undefined ? {} : { hotkey: o.hotkey })}
          {...(o.dim === true ? { dimColor: true } : {})}
          {...(o.primary === true ? { variant: 'primary' as const } : {})}
          hover={{ color: o.color ?? 'magenta', bold: true, inverse: false }}
        />
      </Box>
    )
    const keysRow = (row: Hotkey[], n: number) => (
      <Box key={`keys-${n}`} flexDirection="row" columnGap={2}>
        {row.map(k => press(`hk-${k.letter}`, k.label, go(k.intent), { plain: true, dim: true, hotkey: k.letter }))}
      </Box>
    )
    const mdText = (line: MdLine, k: string) =>
      line.length === 0 ? (
        <Text key={k}>{' '}</Text>
      ) : line.length === 1 ? (
        <Text key={k} wrap="truncate-end" {...style(line[0] as Span)}>{(line[0] as Span).text}</Text>
      ) : (
        <Text key={k} wrap="truncate-end">
          {line.map((span, i) => <Text key={`${k}-${i}`} {...style(span)}>{span.text}</Text>)}
        </Text>
      )

    const noticeOut = state.notice === null ? null : (
      <Box flexDirection="column">
        {trimNotice(state.notice.text).split('\n').map((line, i) => (
          <Text key={`notice-${i}`} color={state.notice?.kind === 'error' ? 'red' : 'green'} wrap="truncate-end">{line}</Text>
        ))}
      </Box>
    )
    const rule = (label = '') => <Text dimColor>{ruleLine(width, label)}</Text>

    if (!state.isLoaded) return <Text dimColor>Reading the board…</Text>

    // The full notice screen: the whole text, wrapped and scrollable — the collapsed band of the other screens keeps its cap.
    if (state.screen === 'notice' && state.notice !== null) {
      const parts = noticeParts(state, layout)
      const top = clamp(state.scroll, 0, Math.max(0, parts.lines.length - parts.bodyRows))
      const shown = parts.lines.slice(top, top + parts.bodyRows)
      const blanks = parts.bodyRows - shown.length
      const range = parts.lines.length > parts.bodyRows ? `${top + 1}–${top + shown.length} / ${parts.lines.length}` : ''
      return (
        <Box flexDirection="column">
          <Text bold color="magenta" wrap="truncate-end">Notice</Text>
          {rule()}
          {shown.map((line, i) => (
            <Text key={`notice-line-${top + i}`} color={state.notice?.kind === 'error' ? 'red' : 'green'} wrap="truncate-end">{line}</Text>
          ))}
          {blanks > 0 ? <Box height={blanks} /> : null}
          {rule(range)}
          <Box flexDirection="row" columnGap={1} height={1} overflow="hidden">
            {press('back', '← back', go({ t: 'back' }))}
            {press('up', '▲', go({ t: 'scroll', d: -PAGE }))}
            {press('down', '▼', go({ t: 'scroll', d: PAGE }))}
          </Box>
          {parts.footerRows.map((row, r) => keysRow(row, r))}
        </Box>
      )
    }

    // Story and card screens share a scrolling body.
    if (state.screen === 'card' || state.screen === 'story') {
      const card = openCard(state)
      if (card === undefined) {
        return (
          <Box flexDirection="column">
            <Text color="red">This card is no longer on the board.</Text>
            {press('back', '← back', go({ t: 'back' }))}
            {noticeOut}
            {pack(hotkeys('card'), hotkeyWidth, width, 2).map((row, r) => keysRow(row, r))}
          </Box>
        )
      }
      const parts = cardParts(state, card, layout)
      const top = clamp(state.scroll, 0, Math.max(0, parts.lines.length - parts.bodyRows))
      const shown = parts.lines.slice(top, top + parts.bodyRows)
      const blanks = parts.bodyRows - shown.length
      const range = parts.lines.length > parts.bodyRows ? `${top + 1}–${top + shown.length} / ${parts.lines.length}` : ''
      let n = 0
      return (
        <Box flexDirection="column">
          {parts.title.map((line, i) => <Text key={`title-${i}`} bold color="magenta" wrap="truncate-end">{line}</Text>)}
          {rule()}
          <Text bold wrap="truncate-end">{clean(parts.sub)}</Text>
          {parts.segRows.map((row, r) => (
            <Box key={`segs-${r}`} flexDirection="row">
              {row.flatMap((seg, i) => {
                const id = `seg-${n++}`
                const item = seg.press === undefined ? (
                  <Text key={id} wrap="truncate-end" {...style(seg)}>{seg.text}</Text>
                ) : (
                  press(id, seg.text, go(seg.press), { plain: true })
                )
                return i === 0 ? [item] : [<Text key={`${id}-sep`} dimColor>{' · '}</Text>, item]
              })}
            </Box>
          ))}
          {rule()}
          {shown.map((line, i) => mdText(line, `line-${top + i}`))}
          {blanks > 0 ? <Box height={blanks} /> : null}
          {rule(range)}
          <Box flexDirection="row" columnGap={1} height={1} overflow="hidden">
            {press('back', '← back', go({ t: 'back' }))}
            {press('up', '▲', go({ t: 'scroll', d: -PAGE }))}
            {press('down', '▼', go({ t: 'scroll', d: PAGE }))}
          </Box>
          {parts.actionRows.map((row, r) => (
            <Box key={`actions-${r}`} flexDirection="row" columnGap={1}>
              {row.map(a =>
                a.state === 'off' || (a.key === 'drop' && Input === undefined) ? (
                  <Text key={a.key} dimColor>{`[ ${a.label} ]`}</Text>
                ) : (
                  press(a.key, a.label, go(a.intent), { color: a.color, primary: a.state === 'main' })
                ),
              )}
            </Box>
          ))}
          {noticeOut}
          {state.isDropping && Input !== undefined ? (
            <Box key="drop-field" height={INPUT_ROWS} flexDirection="row" gap={1} overflow="hidden">
              <Input key={DROP_KEY} autoFocus label="reason" placeholder="why is this card obsolete?" onSubmit={value => void drop($, value)} />
              {press('drop-cancel', 'cancel', go({ t: 'drop', phase: 'cancel' }))}
            </Box>
          ) : null}
          {state.isDropping ? <Text dimColor>Enter drops the card; cancel keeps it.</Text> : parts.footerRows.map((row, r) => keysRow(row, r))}
        </Box>
      )
    }

    if (state.screen === 'search') {
      const result = state.search
      const hits = result?.hits ?? []
      const isEditing = state.searchMode === 'edit'
      const { scopeRows, footerRows, room } = searchParts(state, layout)
      const rows = searchRows(hits)
      const { shown, count } = isEditing ? { shown: [], count: '' } : searchWindow(rows, state.cursor, room)
      const query = result?.query ?? ''
      const files = new Set(hits.map(h => h.path)).size
      const summary = query === '' ? 'whole board · case-insensitive' : `${hits.length} results in ${files} files`
      const empty = !isEditing && shown.length === 0 ? 1 : 0
      const spare = isEditing ? 0 : room - shown.length - empty - (count === '' ? 0 : 1)
      let n = 0
      return (
        <Box flexDirection="column">
          <Box flexDirection="row" columnGap={1}>
            {press('back', '← back', go({ t: 'back' }))}
            <Text color="magenta" wrap="truncate-end">{`▤ search${query === '' ? '' : ` · ${fit(clean(query), width - 20)}`}`}</Text>
          </Box>
          {isEditing ? (
            <Box key="search-field" height={INPUT_ROWS} flexDirection="row" overflow="hidden">
              {Input === undefined ? (
                <Text dimColor>Type /board search &lt;text&gt; to search.</Text>
              ) : (
                <Input key={SEARCH_KEY} autoFocus label="/" placeholder="token, case-insensitive" value={query} onSubmit={value => value.trim() === '' ? undefined : void runSearch($, value.trim(), state.scopes)} />
              )}
            </Box>
          ) : null}
          {scopeRows.map((row, r) => (
            <Box key={`scopes-${r}`} flexDirection="row" columnGap={1}>
              {row.map(label => {
                const name = SCOPES[n++] ?? SCOPES[0]
                return press(`scope-${name}`, label, go({ t: 'scope', name }), { color: 'cyan' })
              })}
            </Box>
          ))}
          <Text dimColor wrap="truncate-end">{summary}</Text>
          {isEditing ? null : (
            <Box flexDirection="column">
              {shown.map(r =>
                r.file !== undefined ? (
                  <Text key={r.key} wrap="truncate-start">
                    <Text color="green">{clean(r.file.slice(0, r.file.lastIndexOf('/') + 1))}</Text>
                    <Text bold>{clean(r.file.slice(r.file.lastIndexOf('/') + 1))}</Text>
                    <Text dimColor>{` ${r.count ?? 0}`}</Text>
                  </Text>
                ) : (
                  <Box key={`box:${r.key}`}>
                    <Button
                      key={r.key}
                      plain
                      autoFocus={r.index === state.cursor ? true : undefined}
                      label={fit(`${String(r.hit?.line ?? 0).padStart(4)}  ${clean(r.hit?.text ?? '')}`, width)}
                      onPress={go({ t: 'hit', index: r.index ?? 0 })}
                      hover={{ color: 'magenta', bold: true, inverse: false }}
                    />
                  </Box>
                ),
              )}
              {empty === 1 ? <Text dimColor>{query === '' ? '— type a token —' : '— no match —'}</Text> : null}
              {spare > 0 ? <Box height={spare} /> : null}
              {count === '' ? null : <Text dimColor>{count}</Text>}
            </Box>
          )}
          {result?.truncated ? <Text dimColor>More matches exist than are listed.</Text> : null}
          {noticeOut}
          {footerRows.map((row, r) => keysRow(row, r))}
        </Box>
      )
    }

    // List screen.
    const counts = tabCounts(state.cards, state.session)
    const list = listParts(state, layout)
    const { here, from, to } = list
    const inner = layout.columns - 4
    const where = state.session === null ? '' : state.session.in_repo ? (state.session.repos[0] ?? 'repository without origin') : `${state.session.repos.length} repos under the workspace root`
    return (
      <Box flexDirection="column">
        <Text color="magenta" wrap="truncate-end">{`▤ Kanban · ${clean(where)}`}</Text>
        {tabRows(counts, layout.columns).map((row, r) => (
          <Box key={`tabs-${r}`} flexDirection="row" columnGap={TAB_GAP}>
            {row.map(i => {
              const tab = TABS[i] as (typeof TABS)[number]
              return i === state.tab ? (
                <Text key={`tab-${tab.column}`} bold color="white" backgroundColor="magenta" wrap="truncate-end">{tabLabel(i, counts[i] ?? 0)}</Text>
              ) : (
                press(`tab-${tab.column}`, fit(tabLabel(i, counts[i] ?? 0), width), go({ t: 'tab', index: i }), { plain: true, dim: true })
              )
            })}
          </Box>
        ))}
        {rule()}
        {here.length === 0 ? <Text dimColor>{`Nothing in ${TABS[state.tab]?.name ?? ''} for this session.`}</Text> : null}
        {here.slice(from, to).map((card, k) => {
          const index = from + k
          const meta = metaParts(card)
          const blocked = blocker(card, state.cards)
          const [first, ...rest] = titleLines(card.title, inner)
          return (
            <Box key={`card-${cardKey(card)}`} flexDirection="column" borderStyle="round" borderColor={index === state.cursor ? 'magenta' : undefined} hover={{ borderColor: 'magenta' }} paddingX={1}>
              <Button
                key={cardFocusKey(card)}
                plain
                autoFocus={index === state.cursor ? true : undefined}
                label={first ?? card.slug}
                onPress={go({ t: 'open', key: cardKey(card) })}
                hover={{ color: 'magenta', bold: true, inverse: false }}
              />
              {rest.map((line, i) => <Text key={`rest-${i}`}>{line}</Text>)}
              <Text wrap="truncate-end">
                <Text bold color={meta.color}>{meta.head}</Text>
                <Text dimColor>{meta.tail}</Text>
                <Text dimColor>{' · '}</Text>
                <Text color="magenta">{clean(meta.slug)}</Text>
              </Text>
              {blocked === null ? null : <Text color="red" wrap="truncate-end">{`⊘ blocked by ${clean(blocked)}`}</Text>}
            </Box>
          )
        })}
        {list.pad > 0 ? <Box height={list.pad} /> : null}
        {list.hasCount ? <Text dimColor>{`${from + 1}–${to} of ${here.length}`}</Text> : null}
        {noticeOut}
        {state.isDropping ? <Text dimColor>Enter drops the card; cancel keeps it.</Text> : list.footerRows.map((row, r) => keysRow(row, r))}
      </Box>
    )
  })
}
