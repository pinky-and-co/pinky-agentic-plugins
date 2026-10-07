import { describe, expect, test } from 'claude-code/testing'

import {
  TABS,
  applicable,
  blocker,
  cardActions,
  cardKey,
  cardParts,
  cardSegments,
  inlineSpans,
  listParts,
  packRows,
  renderMarkdown,
  ruleLine,
  scopeLabel,
  searchParts,
  searchRows,
  searchWindow,
  searchArgs,
  tableLines,
  wrapSpans,
  displayWidth,
  initialState,
  isVisible,
  kanbanWorkspaceRef,
  focusKey,
  hotkeys,
  listWindow,
  metaParts,
  parseCards,
  parseSearch,
  parseSession,
  priorityMark,
  reduce,
  sortCards,
  tabBarWidth,
  tabCards,
  tabCounts,
  tabLabel,
  REFOCUS,
  tabRows,
  tierMark,
  titleLines,
  wrapText,
} from '../hooks/board-pane/register'

const ROOT = '/ws'
const LAYOUT = { columns: 70, rows: 30 }

function card(slug: string, column: string, fm: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) {
  return {
    slug,
    board: `${ROOT}/_KANBAN`,
    chantier: 'CHANTIER0007-first',
    chantier_id: '0007',
    column,
    path: `${ROOT}/_KANBAN/CHANTIER0007-first/${column}/${slug}.md`,
    title: `Title of ${slug}`,
    repos: ['acme/alpha'],
    frontmatter: { model: 'sonnet', ...fm },
    priority: 3,
    body: '# Heading\n\nSome body text.',
    story: `${ROOT}/_KANBAN/CHANTIER0007-first/story.md`,
    age_days: 4,
    ...extra,
  }
}

const IN_REPO = { in_repo: true, repos: ['acme/alpha'] }
const OUTSIDE = { in_repo: false, repos: ['acme/alpha', 'acme/beta'] }

describe('board pane helpers', () => {
  test('the tab bar fits one line in a 72-column pane with counters up to 99', () => {
    // 72 columns less the frame's two border cells: 70 cells of body.
    expect(tabBarWidth([99, 99, 99, 99])).toBeLessThanOrEqual(70)
    expect(tabBarWidth([0, 0, 0, 0])).toBeLessThanOrEqual(70)
    expect(tabBarWidth([99, 99, 99, 99])).toBe(66)
    expect(tabLabel(3, 99)).toBe('⏳ in progress (99)')
    expect(tabLabel(0, 120)).toBe('💡 ideas (99+)')
  })

  test('counts emoji as two cells and the priority glyphs as one', () => {
    expect(displayWidth('💡 ideas')).toBe(8)
    expect(displayWidth('📋 todo')).toBe(7)
    expect(displayWidth('⏳📌')).toBe(4)
    expect(displayWidth('▼▽△▲=')).toBe(5)
    expect(displayWidth('⊘ blocked by x')).toBe(14)
  })

  test('maps priorities to glyph and color, tiers to a square', () => {
    expect([1, 2, 3, 4, 5].map(priorityMark)).toEqual([
      { glyph: '▼', color: 'blue' },
      { glyph: '▽', color: 'blue' },
      { glyph: '=', color: 'yellow' },
      { glyph: '△', color: 'red' },
      { glyph: '▲', color: 'red' },
    ])
    expect(['haiku', 'sonnet', 'opus', 'fable', undefined].map(tierMark)).toEqual(['🟦', '🟪', '🟧', '⬜', '⬜'])
  })

  test('the meta row reads glyph, priority, tier, model, age and chantier', () => {
    const c: any = card('a', '1-todo', { model: 'opus' }, { priority: 5, age_days: 12 })
    expect(metaParts(c)).toEqual({ head: '▲ P5', color: 'red', tail: ' · 🟧 opus · 12d · [0007]', slug: 'a' })
    const bare: any = card('b', '1-todo', { model: undefined }, { age_days: null, chantier_id: null })
    expect(metaParts(bare).tail).toBe(' · ⬜ ? · – · [----]')
    const hostile: any = card('c', '1-todo', { model: 'op\u001b[31mus' }, { age_days: 1 })
    expect(metaParts(hostile).tail).not.toContain('\u001b')
  })

  test('filters on the session repos; a card without repos shows only outside a repository', () => {
    const mine: any = card('mine', '1-todo')
    const other: any = card('other', '1-todo', {}, { repos: ['acme/zeta'] })
    const none: any = card('none', '1-todo', {}, { repos: [] })
    const absent: any = card('absent', '1-todo', {}, { repos: null })
    const both: any = card('both', '1-todo', {}, { repos: ['acme/zeta', 'acme/beta'] })
    const done: any = card('done', '4-done')
    expect([mine, other, none, absent, both, done].map(c => isVisible(c, IN_REPO))).toEqual([true, false, false, false, false, false])
    expect([mine, other, none, absent, both, done].map(c => isVisible(c, OUTSIDE))).toEqual([true, false, true, true, true, false])
    expect(isVisible(mine, null)).toBe(false)
  })

  test('sorts by priority descending, then date ascending, then slug', () => {
    const cards: any[] = [
      card('c', '1-todo', { date: '2026-01-02' }, { priority: 3 }),
      card('b', '1-todo', { date: '2026-01-01' }, { priority: 3 }),
      card('a', '1-todo', {}, { priority: 3 }),
      card('z', '1-todo', { date: '2026-05-01' }, { priority: 5 }),
      card('d', '1-todo', { date: '2026-01-01' }, { priority: 3 }),
    ]
    expect(sortCards(cards).map(c => c.slug)).toEqual(['z', 'b', 'd', 'c', 'a'])
  })

  test('tabs hold their own column, four of them, without 4-done', () => {
    const cards: any[] = [card('i', '0-ideas'), card('t1', '1-todo'), card('t2', '1-todo'), card('s', '2-selected-for-dev'), card('p', '3-in-progress'), card('d', '4-done')]
    expect(TABS.map(t => t.column)).toEqual(['0-ideas', '1-todo', '2-selected-for-dev', '3-in-progress'])
    expect(tabCounts(cards, IN_REPO)).toEqual([1, 2, 1, 1])
    expect(tabCards(cards, IN_REPO, 1).map(c => c.slug)).toEqual(['t1', 't2'])
  })

  test('actions apply per column as move-card documents', () => {
    expect(['0-ideas', '1-todo', '2-selected-for-dev', '3-in-progress', '4-done'].map(c => applicable('select', c))).toEqual([false, true, false, false, false])
    expect(['0-ideas', '1-todo', '2-selected-for-dev', '3-in-progress', '4-done'].map(c => applicable('archive', c))).toEqual([false, false, false, true, true])
    expect(['0-ideas', '1-todo', '2-selected-for-dev', '3-in-progress', '4-done'].map(c => applicable('drop', c))).toEqual([true, true, true, true, false])
  })

  test('a card is blocked only while its blocker is open on the board', () => {
    const open: any = card('open', '2-selected-for-dev')
    const done: any = card('done', '4-done')
    const blockedByOpen: any = card('x', '1-todo', { blocked_by: 'open' })
    const blockedByDone: any = card('y', '1-todo', { blocked_by: 'done' })
    const blockedByGone: any = card('z', '1-todo', { blocked_by: 'archived-card' })
    const all = [open, done, blockedByOpen, blockedByDone, blockedByGone]
    expect([blockedByOpen, blockedByDone, blockedByGone, open].map(c => blocker(c, all))).toEqual(['open', null, null, null])
  })

  test('the badge line of a card follows the POC: column, priority, tier, chantier, date, repos, sert, requirements, blocker', () => {
    const target: any = card('next-one', '2-selected-for-dev')
    const c: any = card('a', '1-todo', { model: 'opus', date: '2026-09-14', sert: 'next-one', requirements: ['FUN-1'], blocked_by: 'open-one' }, { priority: 3, repos: ['acme/alpha', 'acme/beta'] })
    const blocker_: any = card('open-one', '2-selected-for-dev')
    expect(cardSegments(c, [c, target, blocker_]).map(g => g.text)).toEqual([
      '1-todo', '= P3', '🟧 opus', '[0007]', 'scoped 2026-09-14', 'acme/alpha', 'acme/beta', 'sert → next-one', 'FUN-1', '⊘ blocked by open-one',
    ])
    const [, prio, , chantier, , , , sert] = cardSegments(c, [c, target, blocker_])
    // The priority token is bold so the yellow `=` reads on a light background; the chantier opens the story, sert its card.
    expect(prio).toMatchObject({ color: 'yellow', bold: true })
    expect(chantier?.press).toEqual({ t: 'story' })
    expect(sert?.press).toEqual({ t: 'open', key: cardKey(target) })
    expect(cardSegments({ ...c, story: null }, [c])[3]?.press).toBeUndefined()
  })

  test('the badge shows the effort after the model, and nothing when the card carries none', () => {
    const withEffort: any = card('a', '1-todo', { model: 'opus', effort: 'low' })
    const without: any = card('b', '1-todo', { model: 'opus' })
    expect(cardSegments(withEffort, [withEffort]).map(g => g.text).slice(2, 4)).toEqual(['🟧 opus', 'effort low'])
    expect(cardSegments(without, [without]).map(g => g.text).some(t => t.startsWith('effort'))).toBe(false)
  })

  test('the action row marks the main verb of each column and greys the ones that do not apply', () => {
    const states = (column: string) => cardActions(card('x', column) as any, false).map(a => `${a.key}:${a.state}`)
    expect(states('0-ideas')).toEqual(['edit:main', 'select:off', 'archive:off', 'drop:normal'])
    expect(states('1-todo')).toEqual(['edit:normal', 'select:main', 'archive:off', 'drop:normal'])
    expect(states('2-selected-for-dev')).toEqual(['edit:main', 'select:off', 'archive:off', 'drop:normal'])
    expect(states('3-in-progress')).toEqual(['edit:normal', 'select:off', 'archive:main', 'drop:normal'])
    expect(cardActions(card('x', '1-todo') as any, false).map(a => a.label)).toEqual(['✎ open in editor', '↑ select', '▣ archive', '✕ drop'])
    expect(cardActions(card('x', '1-todo') as any, true).map(a => a.key)).toEqual(['edit'])
  })

  test('titles take two lines at most, the second cut with an ellipsis', () => {
    expect(titleLines('Short', 20)).toEqual(['Short'])
    expect(titleLines('one two three four five six', 12)).toEqual(['one two', 'three four…'])
    expect(wrapText('abcdefghij', 4)).toEqual(['abcd', 'efgh', 'ij'])
    expect(titleLines('x'.repeat(100), 10).length).toBe(2)
    expect(titleLines('\u001b[31mred\u0007', 20)[0]).toBe(' red')
  })

  test('markdown: a heading loses its hashes and turns magenta bold, a title heading can be dropped', () => {
    expect(renderMarkdown('## Why\n\nBecause.', 40)).toEqual([[{ text: 'Why', color: 'magenta', bold: true }], [], [{ text: 'Because.' }]])
    expect(renderMarkdown('# Title\n\n## Why', 40, { dropTitle: true })).toEqual([[{ text: 'Why', color: 'magenta', bold: true }]])
  })

  test('markdown: bold and inline code in one line are styled spans, their markers gone', () => {
    expect(inlineSpans('a **bold** and `code` end')).toEqual([
      { text: 'a ' }, { text: 'bold', bold: true }, { text: ' and ' }, { text: 'code', color: 'blue' }, { text: ' end' },
    ])
    expect(renderMarkdown('see **this** and `that`', 40)).toEqual([
      [{ text: 'see ' }, { text: 'this', bold: true }, { text: ' and ' }, { text: 'that', color: 'blue' }],
    ])
  })

  test('markdown: a list item wraps under its text, not under its marker', () => {
    expect(renderMarkdown('- alpha beta gamma delta', 12).map(l => l.map(s => s.text).join(''))).toEqual(['• alpha beta', '  gamma', '  delta'])
    expect(renderMarkdown('1. one two three four', 11).map(l => l.map(s => s.text).join(''))).toEqual(['1. one two', '   three', '   four'])
    expect(renderMarkdown('  - nested item here', 40).map(l => l.map(s => s.text).join(''))).toEqual(['  • nested item here'])
  })

  test('markdown: lines of a paragraph join and rewrap at the pane width', () => {
    expect(renderMarkdown('one two\nthree four', 9).map(l => l[0]?.text)).toEqual(['one two', 'three', 'four'])
  })

  test('markdown: hard-wrapped lines join into one paragraph, a blank line keeps paragraphs apart', () => {
    const texts = (source: string, width: number) => renderMarkdown(source, width).map(l => l.map(s => s.text).join(''))
    expect(texts('a\nb', 40)).toEqual(['a b'])
    expect(texts('a\nb\n\n## H', 40)).toEqual(['a b', '', 'H'])
    expect(texts('a\nb\n## H\nc', 40)).toEqual(['a b', '', 'H', 'c'])
  })

  test('markdown: an indented continuation joins its list item under the hanging indent, a flush line ends the item', () => {
    const texts = (source: string, width: number) => renderMarkdown(source, width).map(l => l.map(s => s.text).join(''))
    expect(texts('- alpha beta\n  gamma delta', 12)).toEqual(['• alpha beta', '  gamma', '  delta'])
    expect(texts('- alpha\n  beta\n- next', 40)).toEqual(['• alpha beta', '• next'])
    expect(texts('- alpha\nbeta', 40)).toEqual(['• alpha', 'beta'])
  })

  test('markdown: a table is column-aligned, its separator row gone', () => {
    const lines = renderMarkdown('| Id | Requirement |\n|---|---|\n| FUN-1 | Short |\n| FUN-22 | Longer one |', 40)
    expect(lines.map(l => l[0]?.text)).toEqual(['Id      Requirement', 'FUN-1   Short', 'FUN-22  Longer one'])
    expect(lines[0]?.[0]?.dimColor).toBe(true)
  })

  test('markdown: a table wider than the pane is truncated with ellipses, every line within the width', () => {
    const wide = '| Id | Requirement |\n|---|---|\n| FUN-0027-01 | A very long statement that cannot possibly fit in this narrow pane |'
    const lines = renderMarkdown(wide, 30)
    expect(lines.length).toBe(2)
    for (const l of lines) expect(displayWidth(l[0]?.text ?? '')).toBeLessThanOrEqual(30)
    expect(lines[1]?.[0]?.text).toContain('…')
    expect(tableLines([['a', 'b'], ['1', '2']], 30).map(l => l[0]?.text)).toEqual(['a  b', '1  2'])
  })

  test('markdown: a rule spans the width, code is indented and coloured, comment markers are stripped', () => {
    expect(renderMarkdown('before\n\n---\n\nafter', 6)[2]).toEqual([{ text: '──────', dimColor: true }])
    expect(renderMarkdown('```py\nx = 1\n```', 20)).toEqual([[{ text: '  x = 1', color: 'blue' }]])
    const handoff = 'text\n\n<!-- auto-handoff:start (maintained by x) -->\n## Handoff (auto)\n- a\n<!-- auto-handoff:end -->\n'
    const flat = renderMarkdown(handoff, 40).map(l => l.map(s => s.text).join(''))
    expect(flat).toEqual(['text', '', 'Handoff (auto)', '• a'])
    expect(flat.join('\n')).not.toContain('<!--')
  })

  test('wrapping cuts a word wider than the line and keeps styles across the cut', () => {
    expect(wrapSpans([{ text: 'abcdefghij', bold: true }], 4).map(l => l[0]?.text)).toEqual(['abcd', 'efgh', 'ij'])
    expect(wrapSpans([{ text: 'xx' }, { text: ' ' }, { text: 'yyyy', bold: true }], 4).map(l => l.map(s => s.text))).toEqual([['xx'], ['yyyy']])
  })

  test('packRows fills rows left to right within the width, the gap between items', () => {
    expect(packRows([10, 10, 10], 21, 1)).toEqual([[0, 1], [2]])
    expect(packRows([10, 10, 10], 32, 1)).toEqual([[0, 1, 2]])
    expect(packRows([], 10, 1)).toEqual([[]])
    expect(ruleLine(8)).toBe('────────')
    expect(ruleLine(14, '1–5 / 9')).toBe('── 1–5 / 9 ───')
  })

  test('the list window always shows the cursor and never more than the rows', () => {
    const heights = [5, 5, 5, 5, 5, 5]
    expect(listWindow(heights, 0, 12)).toEqual([0, 2])
    expect(listWindow(heights, 5, 12)).toEqual([4, 6])
    expect(listWindow(heights, 3, 5)).toEqual([3, 4])
    expect(listWindow([], 0, 12)).toEqual([0, 0])
  })

  test('parses board_data output field by field and rejects what is not a card', () => {
    const good = card('a', '1-todo')
    expect(parseCards(JSON.stringify([good, { slug: 'broken' }, 7]))).toEqual([good])
    expect(parseCards('not json')).toBeNull()
    expect(parseCards('{}')).toBeNull()
    expect(parseCards('[]')).toEqual([])
    expect(parseSession('{"in_repo": false, "repos": ["a/b", 3]}')).toEqual({ in_repo: false, repos: ['a/b'] })
    expect(parseSession('{"repos": []}')).toBeNull()
    const hit = { scope: 'logs', path: '/p', rel: 'p', line: 3, text: 'x' }
    expect(parseSearch(JSON.stringify({ query: 't', counts: { kanban: 1, logs: 'bad' }, hits: [hit, {}], truncated: true }))).toEqual({
      query: 't',
      counts: { kanban: 1 },
      hits: [hit],
      truncated: true,
    })
    expect(parseSearch('[]')).toBeNull()
  })
})

describe('board pane navigation', () => {
  const cards: any[] = [card('a', '1-todo'), card('b', '1-todo'), card('c', '3-in-progress')]
  const loaded = { ...initialState, cards, session: IN_REPO, isLoaded: true, tab: 1 }

  test('each screen lists its letter keys once, each one lowercase letter', () => {
    const letters = (screen: 'list' | 'card' | 'story' | 'search') => hotkeys(screen).map(k => k.letter)
    expect(letters('list')).toEqual(['h', 'l', 'j', 'k', 's', 'f', 'r'])
    expect(letters('card')).toEqual(['j', 'k', 'h', 'e', 's', 'f', 'r'])
    expect(letters('story')).toEqual(['j', 'k', 'h', 'e', 'f', 'r'])
    expect(letters('search')).toEqual([])
    expect(hotkeys('search', 'results').map(k => k.letter)).toEqual(['h', 'j', 'k', 'f', 'r'])
    for (const screen of ['list', 'card', 'story', 'search'] as const) {
      expect(new Set(letters(screen)).size).toBe(letters(screen).length)
      expect(letters(screen).every(l => /^[a-z]$/.test(l))).toBe(true)
    }
  })

  test('moves between states and cards, clamped', () => {
    expect(reduce(loaded, { t: 'tabs', d: 1 }, LAYOUT).state.tab).toBe(2)
    expect(reduce({ ...loaded, tab: 3 }, { t: 'tabs', d: 1 }, LAYOUT).state.tab).toBe(3)
    expect(reduce(loaded, { t: 'cursor', d: 5 }, LAYOUT).state.cursor).toBe(1)
    expect(reduce(loaded, { t: 'cursor', d: -1 }, LAYOUT).state.cursor).toBe(0)
  })

  test('opens the card under the cursor by slug and comes back to its column', () => {
    const opened = reduce({ ...loaded, cursor: 1 }, { t: 'open' }, LAYOUT).state
    expect(opened).toMatchObject({ screen: 'card', cardKey: cardKey(cards[1]) })
    const moved = { ...opened, cards: cards.map(c => (c.slug === 'b' ? { ...c, column: '3-in-progress' } : c)) }
    // The slug still addresses the card after a move between columns.
    expect(reduce(moved, { t: 'back' }, LAYOUT).state).toMatchObject({ screen: 'list', tab: 3 })
  })

  test('select acts on a todo card; a refusal of applicability is said, not run', () => {
    const open = { ...loaded, screen: 'card' as const, cardKey: cardKey(cards[0]) }
    expect(reduce(open, { t: 'act', verb: 'select' }, LAYOUT).effect).toEqual({ t: 'act', verb: 'select', slug: 'a' })
    const wrong = reduce({ ...open, cardKey: cardKey(cards[2]) }, { t: 'act', verb: 'select' }, LAYOUT)
    expect(wrong.effect).toBeUndefined()
    expect(wrong.state.notice).toEqual({ kind: 'error', text: 'select does not apply to a card in 3-in-progress.' })
    expect(reduce({ ...open, cardKey: cardKey(cards[2]) }, { t: 'act', verb: 'archive' }, LAYOUT).effect).toEqual({ t: 'act', verb: 'archive', slug: 'c' })
  })

  test('story needs a story file; the editor line carries the card path', () => {
    const open = { ...loaded, screen: 'card' as const, cardKey: cardKey(cards[0]) }
    expect(reduce(open, { t: 'story' }, LAYOUT).effect).toEqual({ t: 'story', path: `${ROOT}/_KANBAN/CHANTIER0007-first/story.md` })
    const noStory = { ...open, cards: cards.map(c => ({ ...c, story: null })) }
    expect(reduce(noStory, { t: 'story' }, LAYOUT).state.notice?.text).toBe('This chantier has no story.md.')
    expect(reduce(open, { t: 'editor' }, LAYOUT).effect).toEqual({ t: 'editor', path: cards[0].path, line: 1 })
  })

  const items = (n: number) => Array.from({ length: n }, (_, i) => `- line ${i}`).join('\n')
  const opened = (c: any, extra: Record<string, unknown> = {}) => ({ ...loaded, cards: [c], screen: 'card' as const, cardKey: cardKey(c), ...extra })

  test('the body gets every row the pane has, less the rows the screen draws around it', () => {
    const c: any = card('a', '1-todo', {}, { body: items(100) })
    // title, rule, slug, one badge row, two rules, navigation, one action row, one footer row.
    const plain = cardParts(opened(c), c, LAYOUT)
    expect([plain.chrome, plain.bodyRows, plain.chrome + plain.bodyRows]).toEqual([9, 21, 30])
    expect(plain.lines.length).toBe(100)
    // A two-line notice takes two rows from the body.
    const noted = cardParts(opened(c, { notice: { kind: 'error', text: 'one\ntwo' } }), c, LAYOUT)
    expect([noted.chrome, noted.bodyRows]).toEqual([11, 19])
    // The drop field replaces the footer by one hint row and holds INPUT_ROWS (3) for itself.
    const dropping = cardParts(opened(c, { isDropping: true }), c, LAYOUT)
    expect([dropping.chrome, dropping.bodyRows]).toEqual([12, 18])
    // A narrower pane wraps the badge, action and footer rows: fewer rows for the body, never more than the pane has.
    const narrow = cardParts(opened(c), c, { columns: 36, rows: 30 })
    expect(narrow.chrome).toBeGreaterThan(plain.chrome)
    expect(narrow.chrome + narrow.bodyRows).toBe(30)
    expect(cardParts(opened(c), c, { columns: 70, rows: 60 }).bodyRows).toBe(51)
  })

  test('the story screen counts its own rows: one badge, one action', () => {
    const c: any = card('a', '1-todo')
    const story = cardParts({ ...opened(c), screen: 'story', storyText: '# Story title\n\n## Objective\n\nText.' }, c, LAYOUT)
    expect(story.title).toEqual(['Story title'])
    expect(story.sub).toBe('_KANBAN/CHANTIER0007-first/story.md')
    expect(story.lines.map(l => l.map(sp => sp.text).join(''))).toEqual(['Objective', '', 'Text.'])
    // title, rule, sub, badge, rule, rule, navigation, one action, one footer row.
    expect([story.chrome, story.bodyRows]).toEqual([9, 21])
  })

  test('scrolls a body inside its bounds, to the last line the body window shows', () => {
    const long: any = card('a', '1-todo', {}, { body: items(100) })
    const open = opened(long)
    expect(reduce(open, { t: 'scroll', d: -5 }, LAYOUT).state.scroll).toBe(0)
    expect(reduce(open, { t: 'scroll', d: 10 }, LAYOUT).state.scroll).toBe(10)
    expect(reduce(open, { t: 'scroll', d: 1000 }, LAYOUT).state.scroll).toBe(100 - 21)
    expect(reduce(opened(card('a', '1-todo') as any), { t: 'scroll', d: 1000 }, LAYOUT).state.scroll).toBe(0)
  })

  test('the list takes the whole height: cards that fit, a count row, the footer on the last row', () => {
    const many: any[] = Array.from({ length: 12 }, (_, i) => card(`c${i}`, '1-todo', {}, { title: `Card ${i}` }))
    const s = { ...loaded, cards: many }
    const parts = listParts(s, LAYOUT)
    // Each card is 4 rows (border, title, meta, border); title row, tabs, rule and 2 footer rows are drawn around.
    expect(parts.heights.every(h => h === 4)).toBe(true)
    expect(parts.footerRows.length).toBe(2)
    expect(parts.to - parts.from).toBe(6)
    expect(parts.hasCount).toBe(true)
    expect(3 + parts.footerRows.length + (parts.to - parts.from) * 4 + 1 + parts.pad).toBe(LAYOUT.rows)
    const few = listParts({ ...s, cards: many.slice(0, 2) }, LAYOUT)
    expect([few.hasCount, few.pad]).toEqual([false, LAYOUT.rows - 3 - 2 - 8])
  })

  test('a tab bar that wraps to two rows still leaves chrome, cards and footer summing to the pane rows', () => {
    const narrow = { columns: 40, rows: 30 }
    const many: any[] = Array.from({ length: 12 }, (_, i) => card(`c${i}`, '1-todo', {}, { title: `Card ${i}` }))
    const counts = tabCounts(many, IN_REPO)
    const tabs = tabRows(counts, narrow.columns).length
    expect(tabs).toBeGreaterThan(1)
    const parts = listParts({ ...loaded, cards: many }, narrow)
    expect(2 + tabs + parts.footerRows.length + parts.heights.slice(parts.from, parts.to).reduce((n, h) => n + h, 0) + 1 + parts.pad).toBe(narrow.rows)
    const few = listParts({ ...loaded, cards: many.slice(0, 2) }, narrow)
    expect(2 + tabs + few.footerRows.length + few.heights.reduce((n, h) => n + h, 0) + few.pad).toBe(narrow.rows)
  })

  test('any intent but a drop one leaves the drop field, so a reason cannot be submitted from another screen', () => {
    const c: any = card('a', '1-todo')
    const dropping = { ...loaded, screen: 'card' as const, cardKey: cardKey(c), isDropping: true }
    for (const intent of [{ t: 'story' }, { t: 'scroll', d: 10 }, { t: 'back' }, { t: 'act', verb: 'select' }, { t: 'find' }, { t: 'editor' }, { t: 'refresh' }] as const) {
      expect(reduce(dropping, intent, LAYOUT).state.isDropping).toBe(false)
    }
    expect(reduce(dropping, { t: 'drop', phase: 'start' }, LAYOUT).state.isDropping).toBe(true)
    expect(reduce(dropping, { t: 'drop', phase: 'cancel' }, LAYOUT).state.isDropping).toBe(false)
  })

  test('the search screen reserves the field and scope rows, and labels scopes with a box and a count', () => {
    expect(['kanban', 'archive', 'logs'].map((n, i) => scopeLabel(n, i !== 1, i))).toEqual(['☑ kanban 0', '☐ archive 1', '☑ logs 2'])
    const s = { ...loaded, screen: 'search' as const, search: { query: 'q', counts: {}, hits: [], truncated: true } }
    const parts = searchParts(s, LAYOUT)
    expect(parts.scopeRows.length).toBe(1)
    // nav, field (3), scopes (1), summary, truncation note.
    expect(parts.room).toBe(LAYOUT.rows - (1 + 3 + 1 + 1 + 1))
  })

  test('a search scope toggle searches again, the last scope stays on', () => {
    const search = { query: 't', counts: {}, hits: [], truncated: false }
    const s = { ...loaded, screen: 'search' as const, searchMode: 'results' as const, search, scopes: ['kanban', 'logs'] }
    expect(reduce(s, { t: 'scope', name: 'logs' }, LAYOUT).effect).toEqual({ t: 'search', query: 't', scopes: ['kanban'] })
    expect(reduce(s, { t: 'scope', name: 'archive' }, LAYOUT).effect).toEqual({ t: 'search', query: 't', scopes: ['kanban', 'archive', 'logs'] })
    expect(reduce({ ...s, scopes: ['kanban'] }, { t: 'scope', name: 'kanban' }, LAYOUT).effect).toBeUndefined()
  })

  test('in edit mode a scope toggle only changes the scopes, even with a previous search', () => {
    const search = { query: 'old', counts: {}, hits: [], truncated: false }
    for (const prior of [null, search]) {
      const s = { ...loaded, screen: 'search' as const, searchMode: 'edit' as const, search: prior, scopes: ['kanban', 'logs'] }
      const out = reduce(s, { t: 'scope', name: 'logs' }, LAYOUT)
      expect(out.effect).toBeUndefined()
      expect(out.state).toMatchObject({ searchMode: 'edit', scopes: ['kanban'] })
    }
  })

  test('in results mode a scope toggle reruns, but never on an empty query', () => {
    const results = (query: string) => ({ ...loaded, screen: 'search' as const, searchMode: 'results' as const, scopes: ['kanban'], search: { query, counts: {}, hits: [], truncated: false } })
    expect(reduce(results('t'), { t: 'scope', name: 'logs' }, LAYOUT).effect).toEqual({ t: 'search', query: 't', scopes: ['kanban', 'logs'] })
    expect(reduce(results(''), { t: 'scope', name: 'logs' }, LAYOUT).effect).toBeUndefined()
  })

  test('search opens from a screen and goes back to it, the list cursor kept', () => {
    const fromList = reduce({ ...loaded, cursor: 1 }, { t: 'find' }, LAYOUT).state
    expect(fromList).toMatchObject({ screen: 'search', searchFrom: { screen: 'list', cursor: 1 }, search: { query: '', hits: [] } })
    expect(reduce(fromList, { t: 'back' }, LAYOUT).state).toMatchObject({ screen: 'list', cursor: 1 })
    const open = { ...loaded, screen: 'card' as const, cardKey: cardKey(cards[0]) }
    const fromCard = reduce(open, { t: 'find' }, LAYOUT).state
    expect(reduce(fromCard, { t: 'back' }, LAYOUT).state).toMatchObject({ screen: 'card', cardKey: cardKey(cards[0]) })
    // Finding again from the search screen does not lose where it came from.
    expect(reduce(fromCard, { t: 'find' }, LAYOUT).state.searchFrom.screen).toBe('card')
  })

  test('a search hit opens the editor at its line', () => {
    const hit = { scope: 'kanban', path: '/p/a.md', rel: 'a.md', line: 42, text: 'x' }
    const s = { ...loaded, screen: 'search' as const, search: { query: 't', counts: {}, hits: [hit], truncated: false } }
    expect(reduce(s, { t: 'hit', index: 0 }, LAYOUT).effect).toEqual({ t: 'editor', path: '/p/a.md', line: 42 })
  })
})

// Stands for the engine beneath the mod: the scripts answer from a table, the card ops script can refuse.
type Engine = { cards?: unknown; session?: unknown; opExit?: number; opErr?: string; ran?: string[][]; cmux?: 'ok' | 'fail'; kanbanRef?: string; cmuxFail?: string; inCmux?: boolean; code?: 'ok' | 'fail' | 'throw'; open?: 'ok' | 'fail'; hits?: unknown[]; cat?: 'throw'; late?: Record<string, Promise<void>> }
function engine(on: any, opts: Engine = {}) {
  on('process.run', async (_$: any, e: any) => {
    const [exe, script, verb] = e.argv as string[]
    // A search can be held back per query, to land after the screen changed.
    if (verb === 'search') await opts.late?.[e.argv.at(-1) as string]
    opts.ran?.push(e.argv)
    if (exe === 'cmux') {
      const sub = e.argv[1] === 'workspace' ? e.argv[2] : e.argv[1]
      if (opts.cmuxFail === sub) return { value: { exitCode: 1, stdout: '', stderr: 'cmux failed' } }
      if (sub === 'list') {
        const workspaces = [{ ref: 'workspace:1', custom_title: null }, { ref: 'workspace:2', custom_title: 'kanban work' }, ...(opts.kanbanRef !== undefined ? [{ ref: opts.kanbanRef, custom_title: 'kanban' }] : [])]
        return { value: { exitCode: 0, stdout: JSON.stringify({ window_ref: 'window:1', workspaces }), stderr: '' } }
      }
      if (sub === 'new-workspace') opts.kanbanRef = 'workspace:9'
      return { value: { exitCode: opts.cmux === 'fail' ? 1 : 0, stdout: '', stderr: '' } }
    }
    if (exe === 'code') {
      if (opts.code === 'throw') throw new Error('spawn code ENOENT')
      return { value: { exitCode: opts.code === 'fail' ? 127 : 0, stdout: '', stderr: '' } }
    }
    if (exe === 'open') return { value: { exitCode: opts.open === 'fail' ? 1 : 0, stdout: '', stderr: '' } }
    if (exe === 'cat') return { value: { exitCode: 0, stdout: '# Story\nThe story text.', stderr: '' } }
    if (script?.endsWith('board_data.py') && verb === 'search') return { value: { exitCode: 0, stdout: JSON.stringify({ query: opts.late ? e.argv.at(-1) : 'q', counts: {}, hits: opts.late ? [{ scope: 'kanban', path: '/b/a.md', rel: 'a.md', line: 1, text: `found ${e.argv.at(-1)}` }] : opts.hits ?? [], truncated: false }), stderr: '' } }
    if (script?.endsWith('board_data.py') && verb === 'cards') return { value: { exitCode: 0, stdout: JSON.stringify(opts.cards ?? []), stderr: '' } }
    if (script?.endsWith('board_data.py') && verb === 'session-repos') {
      return { value: { exitCode: 0, stdout: JSON.stringify(opts.session ?? IN_REPO), stderr: '' } }
    }
    if (script?.endsWith('pc_kanban_card_ops.py')) {
      return { value: { exitCode: opts.opExit ?? 0, stdout: opts.opExit ? '' : `${verb}ed -> done`, stderr: opts.opErr ?? '' } }
    }
    return { value: { exitCode: 0, stdout: '[]', stderr: '' } }
  })
  on('env.get', () => ({ value: opts.inCmux === true ? 'surface:1' : undefined }))
  on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
  on('session.root', () => ({ value: ROOT }))
  on('command.register', () => ({ value: { command: 'board' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{''}</Text>
  })
}

const PANE = {
  component: 'Pane',
  requestId: 'board',
  props: { title: 'Board', isFocused: true, bodyColumns: 70, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
}
const run = ($: any, args: string) =>
  $.command.run({ command: 'board', args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 72 } } as any)
const open = (c: { slug: string; chantier: string }) => `card:${ROOT}/_KANBAN|${c.chantier}|${c.slug}`
const mount = ($: any) => $.ui.mount({ plugin: 'pc-kanban', surface: 'terminal', ...PANE })

describe('board pane', () => {
  const todo = card('build-widget', '1-todo', { priority: '4' }, { priority: 4, title: 'Build the widget' })
  const doing = card('ship-it', '3-in-progress', {}, { title: 'Ship it' })
  const idea = card('an-idea', '0-ideas', {}, { title: 'An idea' })
  const foreign = card('foreign', '1-todo', {}, { repos: ['acme/zeta'], title: 'Foreign card' })

  test('/board registers on session start, opens the pane and draws the four tabs on one row', async ($, on) => {
    engine(on, { cards: [todo, doing, idea, foreign] })
    await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: true } as any)
    const out: any = await run($, '')
    expect(out.text).toBe('Board pane opened.')
    const ui = await mount($)
    // The active tab is a magenta Text, the others are Buttons.
    expect(await ui.find({ type: 'Text', text: '💡 ideas (1)' })).toBeDefined()
    for (const label of ['📋 todo (1)', '📌 selected (0)', '⏳ in progress (1)']) {
      expect(await ui.find({ type: 'Button', text: label })).toBeDefined()
    }
    expect(await ui.find({ type: 'Text', text: 'Foreign card' })).toBeUndefined()
    await ui.unmount()
  })

  test('the first tab is ideas; a card row shows its title, meta row and blocker', async ($, on) => {
    const blocked = card('blocked', '0-ideas', { blocked_by: 'ship-it' }, { title: 'Blocked idea', priority: 5 })
    engine(on, { cards: [idea, blocked, doing] })
    await run($, '')
    const ui = await mount($)
    expect(await ui.find({ type: 'Text', text: '💡 ideas (2)' })).toBeDefined()
    expect(await ui.find({ type: 'Button', text: 'Blocked idea' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '▲ P5' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '⊘ blocked by ship-it' })).toBeDefined()
    await ui.unmount()
  })

  test('opens a card, shows its body, and a refused select shows the script\'s reason', async ($, on) => {
    const ran: string[][] = []
    engine(on, { cards: [todo], opExit: 1, opErr: 'build-widget not selected:\n  - no `## Done when` section\n  - no `model:`', ran })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.redraw()
    expect(await ui.find({ type: 'Button', text: 'Build the widget' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'build-widget', color: 'magenta' })).toBeDefined()
    await ui.press({ key: open(todo) })
    await ui.redraw()
    expect(await ui.find({ type: 'Text', text: 'Some body text.' })).toBeDefined()
    // The `# Heading` line is the title, not a body line; no markdown source reaches the screen.
    expect(await ui.find({ type: 'Text', text: /^#/ })).toBeUndefined()
    expect(await ui.find({ type: 'Button', key: 'edit', text: '✎ open in editor' })).toBeDefined()
    await ui.press({ key: 'select' })
    await ui.redraw()
    expect(ran.some(argv => argv.slice(-3).join(' ') === '-- select build-widget')).toBe(true)
    // One Text per line of the refusal, each matched whole.
    expect(await ui.find({ type: 'Text', text: /^build-widget not selected:$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}- no `## Done when` section$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}- no `model:`$/ })).toBeDefined()
    await ui.unmount()
  })

  test('select reads the board again; the card stays open in its new column', async ($, on) => {
    const ran: string[][] = []
    engine(on, { cards: [todo], ran })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.press({ key: open(todo) })
    const before = ran.filter(argv => argv[2] === 'cards').length
    await ui.press({ key: 'select' })
    expect(ran.filter(argv => argv[2] === 'cards').length).toBe(before + 1)
    await ui.unmount()
  })

  test('the list draws letter-key Buttons; pressing them moves the tab, the selection and refreshes', async ($, on) => {
    const ran: string[][] = []
    const second = card('second', '1-todo', {}, { title: 'Second card' })
    engine(on, { cards: [todo, second], ran })
    await run($, '')
    const ui = await mount($)
    for (const [letter, label] of [['h', '◀ state'], ['l', 'state ▶'], ['j', 'next'], ['k', 'prev'], ['s', 'story'], ['f', 'search'], ['r', 'refresh']]) {
      expect(await ui.find({ type: 'Button', key: `hk-${letter}`, text: label })).toBeDefined()
    }
    await ui.press({ key: 'hk-l' })
    await ui.redraw()
    expect(await ui.find({ type: 'Text', text: '📋 todo (2)' })).toBeDefined()
    // The card under the cursor is the one whose Button holds `autoFocus`.
    expect(await ui.find({ type: 'Button', key: open(todo), autoFocus: true })).toBeDefined()
    await ui.press({ key: 'hk-j' })
    await ui.redraw()
    expect(await ui.find({ type: 'Button', key: open(second), autoFocus: true })).toBeDefined()
    await ui.press({ key: 'hk-k' })
    await ui.redraw()
    expect(await ui.find({ type: 'Button', key: open(todo), autoFocus: true })).toBeDefined()
    const before = ran.filter(argv => argv[2] === 'cards').length
    await ui.press({ key: 'hk-r' })
    expect(ran.filter(argv => argv[2] === 'cards').length).toBe(before + 1)
    await ui.press({ key: 'hk-h' })
    await ui.redraw()
    expect(await ui.find({ type: 'Text', text: '💡 ideas (0)' })).toBeDefined()
    await ui.unmount()
  })

  test('the focus ring goes to the selected card of the list, or to the drop field', () => {
    const second: any = card('second', '1-todo', {}, { title: 'Second card' })
    const list = { ...initialState, cards: [card('a', '1-todo'), second] as any[], session: IN_REPO, isLoaded: true, tab: 1 }
    expect(focusKey(list)).toBe(`card:${cardKey(list.cards[0]!)}`)
    expect(focusKey(reduce(list, { t: 'cursor', d: 1 }, LAYOUT).state)).toBe(`card:${cardKey(second)}`)
    expect(focusKey({ ...list, cursor: 9 })).toBe(`card:${cardKey(second)}`)
    expect(focusKey({ ...list, tab: 0 })).toBeUndefined()
    expect(focusKey({ ...list, screen: 'card', cardKey: cardKey(second) })).toBeUndefined()
    expect(focusKey({ ...list, screen: 'card', isDropping: true })).toBe('drop-reason')
    // `f` moves the ring to the search field: the screen names it and the intent refocuses.
    expect(focusKey(reduce(list, { t: 'find' }, LAYOUT).state)).toBe('search-input')
    expect(REFOCUS).toContain('find')
  })

  test('the selected card button takes the ring at once and Enter-open is its press', async ($, on) => {
    engine(on, { cards: [todo, card('second', '1-todo', {}, { title: 'Second card' })] })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    expect(await ui.find({ type: 'Button', key: open(todo), autoFocus: true })).toBeDefined()
    await ui.press({ key: open(todo) })
    await ui.redraw()
    expect(await ui.find({ type: 'Button', text: '← back' })).toBeDefined()
    // The card screen: j scrolls, h goes back to the list on the card's tab.
    await ui.press({ key: 'hk-h' })
    await ui.redraw()
    expect(await ui.find({ type: 'Button', key: open(todo) })).toBeDefined()
    await ui.unmount()
  })

  test('the card screen has its own letter keys, and none while a reason is typed', async ($, on) => {
    engine(on, { cards: [todo] })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.press({ key: open(todo) })
    await ui.redraw()
    for (const letter of ['j', 'k', 'h', 'e', 's', 'f', 'r']) expect(await ui.find({ type: 'Button', key: `hk-${letter}` })).toBeDefined()
    await ui.press({ key: 'drop' })
    await ui.redraw()
    expect(await ui.find({ type: 'Input', key: 'drop-reason', autoFocus: true })).toBeDefined()
    for (const letter of ['j', 'k', 'h', 'e', 's', 'f', 'r']) expect(await ui.find({ type: 'Button', key: `hk-${letter}` })).toBeUndefined()
    await ui.unmount()
  })

  test('drop asks for a reason in a field, then runs the script with it', async ($, on) => {
    const ran: string[][] = []
    engine(on, { cards: [todo], ran })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.press({ key: open(todo) })
    await ui.press({ key: 'drop' })
    await ui.redraw()
    expect(await ui.find({ type: 'Input', key: 'drop-reason' })).toBeDefined()
    await ui.input({ key: 'drop-reason', text: 'superseded by another card' })
    expect(ran.some(argv => argv.slice(-4).join('|') === '--|drop|build-widget|superseded by another card')).toBe(true)
    await ui.unmount()
  })

  test('search results are grouped by file with a toggle per scope', async ($, on) => {
    const hit = { scope: 'kanban', path: '/b/a.md', rel: 'CHANTIER0007-first/a.md', line: 12, text: 'the widget line' }
    const raw: any = on
    raw('process.run', (_$: any, e: any) => {
      const out = e.argv.includes('search')
        ? { query: 'widget', counts: { kanban: 1, archive: 0, logs: 2 }, hits: [hit], truncated: false }
        : e.argv.includes('session-repos') ? IN_REPO : []
      return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '' } }
    })
    raw('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
    raw('session.root', () => ({ value: ROOT }))
    raw('ui.open', () => ({ value: { isPlaced: true } }))
    raw('ui.render', ($: any, e: any) => { const { Text } = $.ui.resolve(e); return <Text>{''}</Text> })
    await run($, 'search widget')
    const ui = await mount($)
    expect(await ui.find({ type: 'Text', text: 'CHANTIER0007-first/a.md' })).toBeDefined()
    expect(await ui.find({ type: 'Button', text: '  12  the widget line' })).toBeDefined()
    expect(await ui.find({ type: 'Button', text: '☑ logs 2' })).toBeDefined()
    await ui.press({ key: 'hit-0' })
    await ui.redraw()
    expect(await ui.find({ type: 'Text', text: 'Opened /b/a.md:12' })).toBeDefined()
    await ui.unmount()
  })

  test('an open card fills the pane: 21 body rows of a 30-row pane, the rest of the screen drawn around them', async ($, on) => {
    const long = card('long-one', '1-todo', {}, { body: Array.from({ length: 100 }, (_, i) => `- line ${i}`).join('\n') })
    engine(on, { cards: [long] })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.press({ key: open(long) })
    await ui.redraw()
    expect(await ui.find({ type: 'Text', text: '• line 0' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '• line 20' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '• line 21' })).toBeUndefined()
    for (const key of ['back', 'up', 'down', 'edit', 'select', 'drop']) expect([key, (await ui.find({ type: 'Button', key })) !== undefined]).toEqual([key, true])
    // A verb that does not apply is a grey Text, not a Button.
    expect(await ui.find({ type: 'Button', key: 'archive' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: '[ ▣ archive ]' })).toBeDefined()
    await ui.unmount()
  })

  test('f opens the search screen with its field focused, and back returns to the card', async ($, on) => {
    engine(on, { cards: [todo] })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.press({ key: open(todo) })
    await ui.press({ key: 'hk-f' })
    await ui.redraw()
    expect(await ui.find({ type: 'Input', key: 'search-input', autoFocus: true })).toBeDefined()
    expect(await ui.find({ type: 'Button', key: 'scope-kanban', text: '☑ kanban 0' })).toBeDefined()
    await ui.press({ key: 'back' })
    await ui.redraw()
    expect(await ui.find({ type: 'Button', key: 'select' })).toBeDefined()
    await ui.unmount()
  })

  test('a drop reason starting with a dash goes after a double dash, whole', async ($, on) => {
    const ran: string[][] = []
    engine(on, { cards: [todo], ran })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.press({ key: open(todo) })
    await ui.press({ key: 'drop' })
    await ui.redraw()
    await ui.input({ key: 'drop-reason', text: '-h' })
    expect(ran.some(argv => argv.slice(-4).join('|') === '--|drop|build-widget|-h')).toBe(true)
    await ui.unmount()
  })

  test('a search query starting with a dash goes after a double dash', async ($, on) => {
    const ran: string[][] = []
    engine(on, { ran })
    await run($, 'search -dropped')
    expect(ran.some(argv => argv.slice(-3).join(' ') === '--scope kanban,archive,logs -- -dropped'.split(' ').slice(-3).join(' ') || argv.slice(-2).join(' ') === '-- -dropped')).toBe(true)
    expect(ran.some(argv => argv.includes('search') && argv.indexOf('--') > argv.indexOf('--scope') && argv.at(-1) === '-dropped')).toBe(true)
  })

  test('two cards sharing a slug open apart; the script still gets the bare slug', async ($, on) => {
    const ran: string[][] = []
    const first = card('dup', '1-todo', {}, { body: 'First body', path: `${ROOT}/_KANBAN/CHANTIER0007-first/1-todo/dup.md` })
    const second = card('dup', '1-todo', {}, { body: 'Second body', chantier: 'CHANTIER0008-second', chantier_id: '0008', path: `${ROOT}/_KANBAN/CHANTIER0008-second/1-todo/dup.md` })
    engine(on, { cards: [first, second], ran, opExit: 1, opErr: "ambiguous: 'dup' matches 2 cards" })
    await run($, '')
    const ui = await mount($)
    await ui.press({ key: 'tab-1-todo' })
    await ui.press({ key: open(second) })
    await ui.redraw()
    expect(await ui.find({ type: 'Text', text: 'Second body' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'First body' })).toBeUndefined()
    await ui.press({ key: 'select' })
    await ui.redraw()
    expect(ran.some(argv => argv.slice(-3).join(' ') === '-- select dup')).toBe(true)
    expect(await ui.find({ type: 'Text', text: /^ambiguous: 'dup' matches 2 cards$/ })).toBeDefined()
    await ui.unmount()
  })

  test('open in editor starts VS Code at the line, then the system editor, then shows the command', async ($, on) => {
    const ran: string[][] = []
    const states: Engine[] = [{ code: 'ok' }, { code: 'throw' }, { code: 'fail', open: 'fail' }]
    const wanted = [/^Opened \S+build-widget\.md:1$/, /^Opened \S+build-widget\.md$/, /^No editor could be started\. Open it with: \$EDITOR \+1 \S+build-widget\.md$/]
    const path = todo.path
    const opts: Engine = { cards: [todo], ran }
    engine(on, opts)
    for (const [i, state] of states.entries()) {
      ran.length = 0
      Object.assign(opts, { code: undefined, open: undefined }, state)
      await run($, '')
      const ui = await mount($)
      await ui.press({ key: 'tab-1-todo' })
      await ui.press({ key: open(todo) })
      await ui.press({ key: 'hk-e' })
      await ui.redraw()
      expect(ran.filter(argv => argv[0] === 'code' || argv[0] === 'open')).toEqual(
        [[['code', '-g', `${path}:1`]], [['code', '-g', `${path}:1`], ['open', '-t', path]], [['code', '-g', `${path}:1`], ['open', '-t', path]]][i],
      )
      expect(await ui.find({ type: 'Text', text: wanted[i] })).toBeDefined()
      await ui.unmount()
    }
  })

  describe('opening a file in cmux', () => {
    const setup = ($: any, on: any, opts: Engine) => {
      const ran: string[][] = []
      engine(on, { cards: [todo], ran, inCmux: true, ...opts })
      return async () => {
        ran.length = 0
        await run($, '')
        const ui = await mount($)
        await ui.press({ key: 'tab-1-todo' })
        await ui.press({ key: open(todo) })
        await ui.press({ key: 'hk-e' })
        await ui.redraw()
        await ui.unmount()
        return ran.filter(argv => ['cmux', 'code', 'open'].includes(argv[0] as string))
      }
    }
    const LIST = ['cmux', 'workspace', 'list', '--json']
    const dir = todo.path.slice(0, todo.path.lastIndexOf('/'))
    const CODE = ['code', '-g', `${todo.path}:1`]

    test('an existing kanban workspace is listed then reused, none created', async ($, on) => {
      const edit = setup($, on, { kanbanRef: 'workspace:3' })
      expect(await edit()).toEqual([LIST, ['cmux', 'open', todo.path, '--workspace', 'workspace:3', '--focus', 'true']])
    })

    test('without a kanban workspace one is created, found again by title, then used', async ($, on) => {
      const opts: Engine = {}
      const edit = setup($, on, opts)
      expect(await edit()).toEqual([
        LIST,
        ['cmux', 'new-workspace', '--name', 'kanban', '--cwd', dir, '--focus', 'false'],
        LIST,
        ['cmux', 'open', todo.path, '--workspace', 'workspace:9', '--focus', 'true'],
      ])
    })

    test('a failed list falls to VS Code', async ($, on) => {
      const edit = setup($, on, { kanbanRef: 'workspace:3', cmuxFail: 'list' })
      expect(await edit()).toEqual([LIST, CODE])
    })

    test('a failed open falls to VS Code', async ($, on) => {
      const edit = setup($, on, { kanbanRef: 'workspace:3', cmuxFail: 'open' })
      expect(await edit()).toEqual([LIST, ['cmux', 'open', todo.path, '--workspace', 'workspace:3', '--focus', 'true'], CODE])
    })

    test('outside cmux no cmux call is made', async ($, on) => {
      const edit = setup($, on, { inCmux: false })
      expect(await edit()).toEqual([CODE])
    })
  })

  test('kanbanWorkspaceRef finds the workspace titled exactly kanban', () => {
    const list = (workspaces: unknown[]) => JSON.stringify({ window_ref: 'window:1', workspaces })
    expect(kanbanWorkspaceRef(list([{ ref: 'workspace:1', custom_title: null }, { ref: 'workspace:3', custom_title: 'kanban' }]))).toBe('workspace:3')
    expect(kanbanWorkspaceRef(list([{ ref: 'workspace:1', custom_title: 'Kanban' }, { ref: 'workspace:2', custom_title: 'kanban board' }, { ref: 'workspace:4', custom_title: null }]))).toBeNull()
    expect(kanbanWorkspaceRef(list([]))).toBeNull()
    expect(kanbanWorkspaceRef(list([{ custom_title: 'kanban' }]))).toBeNull()
    expect(kanbanWorkspaceRef('not json')).toBeNull()
    expect(kanbanWorkspaceRef('[]')).toBeNull()
    expect(kanbanWorkspaceRef('{"workspaces":"x"}')).toBeNull()
  })

  test('a search hit opens its file at its line', async ($, on) => {
    const hit = { scope: 'kanban', path: '/b/a.md', rel: 'a.md', line: 12, text: 'x' }
    const ran: string[][] = []
    engine(on, { ran, hits: [hit] })
    await run($, 'search x')
    const ui = await mount($)
    await ui.press({ key: 'hit-0' })
    expect(ran.some(argv => argv.join(' ') === 'code -g /b/a.md:12')).toBe(true)
    await ui.unmount()
  })

  describe('search results mode', () => {
    const many = Array.from({ length: 40 }, (_, i) => ({ scope: 'kanban', path: i < 20 ? '/b/a.md' : '/b/z.md', rel: i < 20 ? 'a.md' : 'z.md', line: i + 1, text: `hit number ${i}` }))
    const letterKeys = ['j', 'k', 'f', 'r']

    test('Enter on the field runs the search and switches to results: hotkeys, no field', async ($, on) => {
      engine(on, { hits: many.slice(0, 3) })
      await run($, '')
      const ui = await mount($)
      await ui.press({ key: 'hk-f' })
      await ui.redraw()
      expect(await ui.find({ type: 'Input', key: 'search-input' })).toBeDefined()
      for (const letter of letterKeys) expect(await ui.find({ type: 'Button', key: `hk-${letter}` })).toBeUndefined()
      await ui.input({ key: 'search-input', text: 'hit' })
      await ui.redraw()
      expect(await ui.find({ type: 'Input', key: 'search-input' })).toBeUndefined()
      for (const letter of ['h', ...letterKeys]) expect(await ui.find({ type: 'Button', key: `hk-${letter}` })).toBeDefined()
      expect(await ui.find({ type: 'Button', key: 'hit-0', autoFocus: true })).toBeDefined()
      await ui.unmount()
    })

    test('j and k move the cursor hit, the window follows it past the first page, headers kept', async ($, on) => {
      engine(on, { hits: many })
      await run($, 'search hit')
      const ui = await mount($)
      expect(await ui.find({ type: 'Input', key: 'search-input' })).toBeUndefined()
      expect(await ui.find({ type: 'Button', key: 'hit-0', autoFocus: true })).toBeDefined()
      expect(await ui.find({ type: 'Button', key: 'hit-39' })).toBeUndefined()
      for (let i = 0; i < 39; i++) await ui.press({ key: 'hk-j' })
      await ui.redraw()
      expect(await ui.find({ type: 'Button', key: 'hit-39', autoFocus: true })).toBeDefined()
      expect(await ui.find({ type: 'Button', key: 'hit-0' })).toBeUndefined()
      expect(await ui.find({ type: 'Text', text: 'z.md 20' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /\/ 40$/ })).toBeDefined()
      await ui.press({ key: 'hk-k' })
      await ui.redraw()
      expect(await ui.find({ type: 'Button', key: 'hit-38', autoFocus: true })).toBeDefined()
      await ui.unmount()
    })

    test('f returns to the field, prefilled with the query; h goes back', async ($, on) => {
      engine(on, { hits: many.slice(0, 3) })
      await run($, 'search q')
      const ui = await mount($)
      await ui.press({ key: 'hk-f' })
      await ui.redraw()
      expect(await ui.find({ type: 'Input', key: 'search-input', value: 'q' })).toBeDefined()
      await ui.press({ key: 'back' })
      await ui.redraw()
      expect(await ui.find({ type: 'Text', text: /▤ Kanban/ })).toBeDefined()
      await ui.unmount()
    })

    test('a search result landing after the user went back is dropped', async ($, on) => {
      let release!: () => void
      const late = { slow: new Promise<void>(r => { release = r }) }
      engine(on, { hits: many.slice(0, 3), late })
      await run($, '')
      const ui = await mount($)
      await ui.press({ key: 'hk-f' })
      await ui.redraw()
      void ui.input({ key: 'search-input', text: 'slow' })
      await ui.press({ key: 'back' })
      await ui.redraw()
      expect(await ui.find({ type: 'Text', text: /▤ Kanban/ })).toBeDefined()
      release()
      await ui.redraw()
      expect(await ui.find({ type: 'Text', text: /▤ Kanban/ })).toBeDefined()
      expect(await ui.find({ type: 'Button', key: 'hit-0' })).toBeUndefined()
      await ui.unmount()
    })

    test('of two overlapping searches the latest-started wins', async ($, on) => {
      let releaseFirst!: () => void
      const late = { first: new Promise<void>(r => { releaseFirst = r }) }
      engine(on, { hits: many.slice(0, 3), late })
      await run($, '')
      const ui = await mount($)
      await ui.press({ key: 'hk-f' })
      await ui.redraw()
      void ui.input({ key: 'search-input', text: 'first' })
      void ui.input({ key: 'search-input', text: 'second' })
      await ui.redraw()
      releaseFirst()
      await ui.redraw()
      expect(await ui.find({ type: 'Text', text: /second/ })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /first/ })).toBeUndefined()
      await ui.unmount()
    })

    test('r reruns the search and a scope toggle too, both with the session repos before the double dash', async ($, on) => {
      const ran: string[][] = []
      engine(on, { ran, hits: many.slice(0, 3) })
      await run($, 'search q')
      const ui = await mount($)
      ran.length = 0
      await ui.press({ key: 'hk-r' })
      await ui.press({ key: 'scope-logs' })
      const searches = ran.filter(argv => argv.includes('search'))
      expect(searches.length).toBe(2)
      for (const argv of searches) {
        expect(argv.indexOf('--repos')).toBeGreaterThan(-1)
        expect(argv[argv.indexOf('--repos') + 1]).toBe('acme/alpha')
        expect(argv.indexOf('--repos')).toBeLessThan(argv.indexOf('--'))
      }
      await ui.unmount()
    })

    test('the repos filter is sent from inside a repository only', () => {
      expect(searchArgs({ in_repo: true, repos: ['a/b', 'c/d'] }, ['kanban'], 'q')).toEqual(['search', '--scope', 'kanban', '--repos', 'a/b,c/d', '--', 'q'])
      expect(searchArgs({ in_repo: false, repos: ['a/b'] }, ['kanban'], 'q')).toEqual(['search', '--scope', 'kanban', '--', 'q'])
      expect(searchArgs({ in_repo: true, repos: [] }, ['kanban'], 'q')).toEqual(['search', '--scope', 'kanban', '--', 'q'])
      expect(searchArgs(null, ['kanban'], 'q')).toEqual(['search', '--scope', 'kanban', '--', 'q'])
    })

    test('the window always holds the cursor, keeps its file header and fits the room', () => {
      const rows = searchRows(many)
      for (const room of [3, 8, 20]) {
        for (let cursor = 0; cursor < many.length; cursor++) {
          const { shown, count } = searchWindow(rows, cursor, room)
          expect(shown.length + 1).toBeLessThanOrEqual(room + 1)
          expect(shown.some(r => r.index === cursor)).toBe(true)
          if (room >= 8 && shown[0]?.hit !== undefined) throw new Error(`no header above hit ${shown[0].index}`)
          expect(count).toMatch(/ \/ 40$/)
        }
      }
      expect(searchWindow(rows.slice(0, 4), 0, 10)).toMatchObject({ count: '' })
    })

    test('the search screen in results mode counts its rows: no field, the footer instead', () => {
      const s = { ...initialState, screen: 'search' as const, searchMode: 'results' as const, search: { query: 'q', counts: {}, hits: [], truncated: false } }
      const parts = searchParts(s, LAYOUT)
      // nav, footer row(s), scopes, summary.
      expect(parts.room).toBe(LAYOUT.rows - (1 + parts.footerRows.length + parts.scopeRows.length + 1))
      expect(focusKey({ ...s, search: { ...s.search, hits: many.slice(0, 2) } })).toBe('hit-0')
    })

    test('find from results is the edit mode again', () => {
      const s = { ...initialState, screen: 'search' as const, searchMode: 'results' as const, search: { query: 'q', counts: {}, hits: [], truncated: false } }
      expect(reduce(s, { t: 'find' }, LAYOUT).state.searchMode).toBe('edit')
    })
  })

  test('/board search opens the pane on the results', async ($, on) => {
    const ran: string[][] = []
    engine(on, { ran })
    const out: any = await run($, 'search widget')
    expect(out.text).toBe('Board pane opened.')
    expect(ran.some(argv => argv.includes('search') && argv.includes('widget'))).toBe(true)
    expect(((await run($, 'search')) as any).text).toBe('Usage: /board  or  /board search <text>')
  })
})
