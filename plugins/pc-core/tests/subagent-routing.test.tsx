import { describe, expect, test } from 'claude-code/testing'

import { cardChoice, holdLimits, ownModel } from '../hooks/subagent-routing/register'
import { tierMark } from '../hooks/subagent-routing/tiers'

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: true, maxRows: 12, bodyColumns: 120 } }
const CARD = '/repo/_KANBAN/C/3-in-progress/x.md'
const GRID = JSON.stringify({
  default: 'logic',
  categories: { logic: { model: 'sonnet', effort: 'medium' } },
})
const ANSWERS: Record<string, unknown> = {
  routine: { category: 'routine', model: 'haiku', effort: 'low' },
  logic: { category: 'logic', model: 'sonnet', effort: 'medium' },
  hard: { category: 'hard', model: 'opus', effort: 'high' },
}

// Stands for the engine beneath the mod: the Agent call runs, the classifier and the journal are faked, nothing real is written.
type Engine = {
  surfaces?: string[]
  classify?: 'throw' | 'garbage' | 'error'
  card?: string
  agents?: Record<string, string>
  failClearOnce?: boolean
  clock?: { t: number }
}
function engine(on: any, opts: Engine = {}) {
  const ran: any[] = []
  const journal: any[] = []
  const classified: string[] = []
  on('tool.call', (_$: any, e: any) => {
    ran.push(e)
    return { result: {}, text: 'ok' }
  })
  on('process.run', async (_$: any, e: any) => {
    const [cmd, script] = e.argv as string[]
    if (script?.endsWith('/classify.py')) {
      classified.push(e.init.stdin)
      if (opts.classify === 'throw') throw new Error('boom')
      if (opts.classify === 'garbage') return { value: { exitCode: 0, stdout: 'not json', stderr: '' } }
      const text: string = e.init.stdin
      if (/slow/.test(text)) await new Promise(done => (globalThis as any).setTimeout(done, 120))
      const key = /unit tests/.test(text) ? 'routine' : /architecture/.test(text) ? 'hard' : 'logic'
      const answer = { ...(ANSWERS[key] as object), model_id: 'clef-flash-4bit', ...(opts.classify === 'error' ? { error: 'HTTP 507' } : {}) }
      return { value: { exitCode: 0, stdout: JSON.stringify(answer), stderr: '' } }
    }
    if (script?.endsWith('/journal.py')) {
      journal.push(JSON.parse(e.init.stdin))
      return { value: { exitCode: 0, stdout: '', stderr: '' } }
    }
    if (cmd === 'sleep') await new Promise(done => (globalThis as any).setTimeout(done, 5)) // a real wait, so the hold loop yields
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('fs.read', (_$: any, e: any) => {
    if (e.path === CARD && opts.card !== undefined) return { value: opts.card }
    if (String(e.path).endsWith('/grid.json')) return { value: GRID }
    if (e.path === '/cfg/plugins/installed_plugins.json') {
      return { value: JSON.stringify({ plugins: { 'guides@mkt': [{ installPath: '/cache/guides' }] } }) }
    }
    const found = Object.entries(opts.agents ?? {}).find(([suffix]) => String(e.path).endsWith(suffix))
    if (found !== undefined) return { value: found[1] }
    throw new Error('missing')
  })
  on('env.get', (_$: any, e: any) => ({ value: e.name === 'CLAUDE_CONFIG_DIR' ? '/cfg' : undefined }))
  const cleared = { thrown: 0 }
  if (opts.failClearOnce) {
    // Only the band's clearing write fails, once; every other write goes on to the engine.
    on('state.set', (_$: any, e: any, next: any) => {
      if (e.value === null && cleared.thrown === 0) {
        cleared.thrown += 1
        throw new Error('state write failed')
      }
      return next(e)
    })
  }
  on('session.root', () => ({ value: '/repo' }))
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  let now = 0
  on('clock.now', () => ({ value: opts.clock ? opts.clock.t : (now += 1000) })) // a clock that moves by itself, so a hold can expire
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.render', ($: any, e: any) => { const { Text } = $.ui.resolve(e); return <Text>{''}</Text> }) // an empty band beneath the mod
  return { ran, journal, classified, cleared }
}

async function heldBand($: any, text: RegExp | string = /Proposed/) {
  for (let i = 0; i < 50; i++) {
    const ui = await $.ui.mount({ plugin: 'pc-core', surface: 'terminal', ...BAND })
    if (await ui.find({ type: 'Text', text })) return ui
    await ui.unmount()
  }
  throw new Error('nothing was held')
}

const agent = (id: string, extra: object = {}) =>
  ({ tool: 'Agent', tool_use_id: id, description: 'task', prompt: 'refactor the loader across files', ...extra }) as any

describe('subagent routing', () => {
  test('the call leaves with the category the classifier ranks', async ($, on) => {
    const { ran } = engine(on, { surfaces: [] })
    await $.tool.call(agent('t1', { prompt: 'write the unit tests of parse_date', model: 'opus' }))
    await $.tool.call(agent('t2', { prompt: 'refactor the loader across files' }))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['haiku', 'low'], ['sonnet', 'medium']])
  })

  test('the session is told the model and effort the call left with', async ($, on) => {
    engine(on)
    const call = $.tool.call(agent('t1', { model: 'sonnet' }))
    const ui = await heldBand($)
    await ui.press({ key: 'model-opus' })
    await ui.press({ key: 'effort-high' })
    await ui.press({ key: 'accept' })
    const done = await call
    await ui.unmount()
    expect(done.context).toEqual([
      'This subagent ran on opus · high (routing: logic, user-choice), which may differ from the model and effort this call named.',
    ])
  })

  test('with no band the session is told the classifier proposal', async ($, on) => {
    engine(on, { surfaces: [] })
    const done = await $.tool.call(agent('t1', { prompt: 'write the unit tests of parse_date', model: 'sonnet' }))
    expect(done.context).toEqual([
      'This subagent ran on haiku · low (routing: routine, no-band), which may differ from the model and effort this call named.',
    ])
  })

  test('the Task name routes like Agent', async ($, on) => {
    const { ran } = engine(on, { surfaces: [] })
    await $.tool.call(agent('t1', { tool: 'Task', prompt: 'design the architecture of the sync' }))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['opus', 'high']])
  })

  test('a failing classifier gives the grid default and an error line in the journal', async ($, on) => {
    const { ran, journal } = engine(on, { surfaces: [], classify: 'throw' })
    await $.tool.call(agent('t1'))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['sonnet', 'medium']])
    expect(journal).toHaveLength(1)
    expect(journal[0].reason).toBe('no-band')
    expect(journal[0].error).toMatch(/./)
  })

  test('the error field of the classifier names the journal reason', async ($, on) => {
    const { journal } = engine(on, { surfaces: [], classify: 'error' })
    await $.tool.call(agent('t1'))
    expect(journal[0].reason).toBe('no-band')
    expect(journal[0].error).toBe('HTTP 507')
    expect(journal[0].classifier_model).toBe('clef-flash-4bit')
    expect(journal[0].category).toBe('logic')
  })

  test('an unreadable classifier answer falls back to the grid default', async ($, on) => {
    const { ran, journal } = engine(on, { surfaces: [], classify: 'garbage' })
    await $.tool.call(agent('t1'))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['sonnet', 'medium']])
    expect(journal[0].reason).toBe('no-band')
    expect(journal[0].error).toMatch(/./)
  })

  test('a classifier failure and no key keep the timeout reason and carry the error', async ($, on) => {
    const { journal } = engine(on, { classify: 'error' })
    const before = holdLimits.ms
    holdLimits.ms = 40
    try {
      await $.tool.call(agent('t1'))
    } finally {
      holdLimits.ms = before
    }
    expect(journal.map(j => [j.reason, j.error])).toEqual([['timeout', 'HTTP 507']])
  })

  const GUIDE = { '/cache/guides/agents/guide.md': '---\nname: guide\nmodel: haiku\n---\nbody' }

  test('a call with no model to an agent whose definition carries one passes untouched', async ($, on) => {
    const { ran, journal, classified } = engine(on, { agents: GUIDE })
    await $.tool.call(agent('t1', { subagent_type: 'guides:guide' }))
    expect(ran.map(e => e.model)).toEqual([undefined])
    expect(classified).toEqual([])
    expect(journal).toEqual([])
  })

  test('the same agent called with a model is held', async ($, on) => {
    const { ran } = engine(on, { agents: GUIDE })
    const call = $.tool.call(agent('t1', { subagent_type: 'guides:guide', model: 'opus' }))
    const ui = await heldBand($, /guides:guide/)
    expect(ran).toEqual([])
    await ui.press({ key: 'accept' })
    await call
    await ui.unmount()
  })

  test('an agent whose definition file is missing is held', async ($, on) => {
    const { ran } = engine(on, { surfaces: [] })
    await $.tool.call(agent('t1', { subagent_type: 'guides:missing' }))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['sonnet', 'medium']])
  })

  test('the working-tree sibling definition wins over the cached one', async ($, on) => {
    const { ran, classified } = engine(on, {
      agents: { ...GUIDE, '/../guides/agents/guide.md': '---\nmodel: sonnet\n---\n' },
    })
    await $.tool.call(agent('t1', { subagent_type: 'guides:guide' }))
    expect(ran.map(e => e.model)).toEqual([undefined])
    expect(classified).toEqual([])
  })

  test('a band that cannot be cleared does not block the next call', async ($, on) => {
    const { ran, cleared, journal } = engine(on, { failClearOnce: true })
    const first = $.tool.call(agent('t1'))
    const ui1 = await heldBand($)
    await ui1.press({ key: 'model-opus' })
    await ui1.press({ key: 'accept' })
    await first
    await ui1.unmount()
    expect(ran.map(e => [e.tool_use_id, e.model])).toEqual([['t1', 'opus']])
    expect(journal.map(j => [j.applied_model, j.reason])).toEqual([['opus', 'user-choice']])
    const second = $.tool.call(agent('t2'))
    const ui2 = await heldBand($)
    await ui2.press({ key: 'accept' })
    await second
    expect(cleared.thrown).toBe(1)
    expect(ran.map(e => e.tool_use_id)).toContain('t2')
    await ui2.unmount()
  })

  test('a project or user agent with a model of its own passes untouched when called without one', async ($, on) => {
    const { ran, journal } = engine(on, {
      agents: {
        '/repo/.claude/agents/pa.md': '---\nmodel: haiku\n---\n',
        '/cfg/agents/ua.md': '---\nmodel: opus\n---\n',
      },
    })
    await $.tool.call(agent('t1', { subagent_type: 'pa' }))
    await $.tool.call(agent('t2', { subagent_type: 'ua' }))
    expect(ran.map(e => e.model)).toEqual([undefined, undefined])
    expect(journal).toEqual([])
  })

  test('calls are shown in arrival order even when the first is classified last', async ($, on) => {
    const { ran } = engine(on)
    const first = $.tool.call(agent('t1', { subagent_type: 'first-agent', prompt: 'slow refactor' }))
    const second = $.tool.call(agent('t2', { subagent_type: 'second-agent' }))
    const ui1 = await heldBand($, /first-agent/)
    expect(await ui1.find({ type: 'Text', text: /second-agent/ })).toBeUndefined()
    await ui1.press({ key: 'accept' })
    await first
    await ui1.unmount()
    const ui2 = await heldBand($, /second-agent/)
    await ui2.press({ key: 'accept' })
    await second
    expect(ran.map(e => e.tool_use_id)).toEqual(['t1', 't2'])
    await ui2.unmount()
  })

  test('reads the model of a definition, inherit being none', () => {
    expect(ownModel('---\nmodel: haiku\n---\n')).toBe('haiku')
    expect(ownModel('---\nmodel: "opus"\n---\n')).toBe('opus')
    expect(ownModel('---\nmodel: inherit\n---\n')).toBeNull()
    expect(ownModel('---\nname: x\n---\nmodel: opus')).toBeNull()
  })

  test('a fork is never routed', async ($, on) => {
    const { ran, journal, classified } = engine(on, { surfaces: [] })
    await $.tool.call(agent('t1', { subagent_type: 'fork', model: 'opus' }))
    expect(ran.map(e => e.model)).toEqual(['opus'])
    expect(classified).toEqual([])
    expect(journal).toEqual([])
  })

  test('the band shows model, effort and category, and the call has not left', async ($, on) => {
    const { ran } = engine(on)
    const call = $.tool.call(agent('t1'))
    const ui = await heldBand($, /Proposed 🟪 sonnet · medium · logic/)
    expect(ran).toEqual([])
    await ui.press({ key: 'accept' })
    await call
    await ui.unmount()
  })

  test('every model in the band carries its tier mark', async ($, on) => {
    engine(on)
    const call = $.tool.call(agent('t1'))
    const ui = await heldBand($)
    for (const [m, mark] of [['haiku', '🟦'], ['sonnet', '🟪'], ['opus', '🟧']]) {
      expect(await ui.find({ key: `model-${m}` })).toBeDefined()
      expect(tierMark(m)).toBe(mark)
    }
    await ui.press({ key: 'accept' })
    await call
    await ui.unmount()
  })

  test('the model and effort picked are those of the call that leaves', async ($, on) => {
    const { ran, journal } = engine(on)
    const call = $.tool.call(agent('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'model-opus' })
    await ui.press({ key: 'effort-high' })
    await ui.press({ key: 'accept' })
    await call
    expect(ran.map(e => [e.model, e.effort])).toEqual([['opus', 'high']])
    expect(journal.map(j => j.reason)).toEqual(['user-choice'])
    await ui.unmount()
  })

  test('no key within the limit sends the proposal and journals a timeout', async ($, on) => {
    const { ran, journal } = engine(on)
    const before = holdLimits.ms
    holdLimits.ms = 40
    try {
      await $.tool.call(agent('t1'))
    } finally {
      holdLimits.ms = before
    }
    expect(ran.map(e => [e.model, e.effort])).toEqual([['sonnet', 'medium']])
    expect(journal.map(j => j.reason)).toEqual(['timeout'])
  })

  test('a card with both values is the proposal and the classifier is not called', async ($, on) => {
    const { ran, classified } = engine(on, { surfaces: [], card: '---\nmodel: opus\neffort: high\n---\nbody' })
    await $.tool.call(agent('t1', { subagent_type: 'pc-kanban:card-worker', prompt: `Card: ${CARD}\nrefactor` }))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['opus', 'high']])
    expect(classified).toEqual([])
  })

  test('a card with no value is classified at launch', async ($, on) => {
    const { ran, classified } = engine(on, { surfaces: [], card: '---\ntitle: x\n---\nbody' })
    await $.tool.call(agent('t1', { subagent_type: 'pc-kanban:card-worker', prompt: `Card: ${CARD}\nwrite the unit tests` }))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['haiku', 'low']])
    expect(classified).toHaveLength(1)
  })

  test('reads only an allowed model and effort from a card', () => {
    expect(cardChoice('---\nmodel: opus\neffort: high\n---\n')).toEqual({ model: 'opus', effort: 'high' })
    expect(cardChoice('---\nmodel: gpt\neffort: max\n---\n')).toEqual({})
    expect(cardChoice('no frontmatter\nmodel: opus')).toEqual({})
  })

  test('a second call is shown after the first answer, and its limit starts when it is shown', async ($, on) => {
    const clock = { t: 0 }
    const { ran } = engine(on, { clock })
    const before = holdLimits.ms
    holdLimits.ms = 1000
    try {
      const first = $.tool.call(agent('t1', { subagent_type: 'first-agent' }))
      const second = $.tool.call(agent('t2', { subagent_type: 'second-agent' }))
      const ui1 = await heldBand($, /first-agent/)
      expect(ran).toEqual([])
      clock.t = 900
      await ui1.press({ key: 'accept' })
      await first
      await ui1.unmount()
      expect(ran.map(e => e.tool_use_id)).toEqual(['t1'])
      const ui2 = await heldBand($, /second-agent/)
      clock.t = 1500 // past 1000 from the arrival of the second call, within 1000 of its display (900)
      await new Promise(done => (globalThis as any).setTimeout(done, 30))
      expect(ran.map(e => e.tool_use_id)).toEqual(['t1'])
      await ui2.press({ key: 'accept' })
      await second
      expect(ran.map(e => e.tool_use_id)).toEqual(['t1', 't2'])
      await ui2.unmount()
    } finally {
      holdLimits.ms = before
    }
  })

  test('with no surface drawing the band the call leaves at once and journals no-band', async ($, on) => {
    const { ran, journal } = engine(on, { surfaces: ['vscode'] })
    await $.tool.call(agent('t1'))
    expect(ran.map(e => [e.model, e.effort])).toEqual([['sonnet', 'medium']])
    expect(journal.map(j => j.reason)).toEqual(['no-band'])
  })
})
