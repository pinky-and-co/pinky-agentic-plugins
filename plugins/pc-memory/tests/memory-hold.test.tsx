import { describe, expect, test } from 'claude-code/testing'

import { clean, descriptionOf, holdLimits, isMemoryNote, parseLevels } from '../hooks/memory-hold/register'

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: true, maxRows: 12, bodyColumns: 120 } }
const DIR = '/Users/p/.claude/projects/-Users-p-Workspaces-pinky-repo/memory'
const PARENT = '/Users/p/.claude/projects/-Users-p-Workspaces-pinky/memory'
const PATH = `${DIR}/x.md`
const NOTE = '---\nname: x\ndescription: Une note sur le niveau workspace\nmetadata:\n  type: user\n---\ncorps'

const LEVELS = [
  { name: 'repo', dir: DIR, current: true, target: true },
  { name: 'pinky', dir: PARENT, current: false, target: false },
]

// Stands for the engine beneath the mod: the write runs, the permission check asks (or answers by path), panes open.
type Engine = { env?: string; surfaces?: string[]; sleepExit?: number; levels?: string; indexExit?: number }
function engine(on: any, ran: string[], opts: Engine = {}) {
  on('tool.call', (_$: any, e: any) => {
    ran.push(e.file_path)
    return { result: {}, text: 'ok' }
  })
  on('tool.check', (_$: any, e: any) => {
    const path: string = e.input.file_path
    if (path.endsWith('/deny.md')) return { decision: 'deny', reason: 'rule' }
    if (path.endsWith('/allow.md')) return { decision: 'allow' }
    if (path.endsWith('/rule.md')) return { decision: 'ask', reason: 'rule', rule: 'Write(*)' }
    return { decision: 'ask', reason: 'guard' }
  })
  on('process.run', async (_$: any, e: any) => {
    if (e.argv[0] === 'python3' && e.argv[1].endsWith('/index_note.py')) {
      return { value: { exitCode: opts.indexExit ?? 0, stdout: 'indexed', stderr: '' } }
    }
    if (e.argv[0] === 'python3') {
      const relative = e.argv[3].startsWith(`${DIR}/`) ? e.argv[3].slice(DIR.length + 1) : null
      const stdout = opts.levels ?? JSON.stringify({ levels: LEVELS, relative })
      return { value: { exitCode: 0, stdout, stderr: '' } }
    }
    if (e.argv[0] === 'sleep') await new Promise(done => (globalThis as any).setTimeout(done, 5)) // a real wait, so the hold loop yields
    return { value: { exitCode: e.argv[0] === 'sleep' ? (opts.sleepExit ?? 0) : 0, stdout: '', stderr: '' } }
  })
  on('env.get', () => ({ value: opts.env }))
  on('session.root', () => ({ value: '/Users/p/Workspaces/pinky/repo' }))
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  let now = 0
  on('clock.now', () => ({ value: (now += 1000) })) // a clock that moves by itself, so a hold can expire
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.render', ($: any, e: any) => { const { Text } = $.ui.resolve(e); return <Text>{''}</Text> }) // la bande vide : rien sous le mod
}

async function heldBand($: any, text: RegExp | string = 'Une note sur le niveau workspace') {
  for (let i = 0; i < 50; i++) {
    const ui = await $.ui.mount({ plugin: 'pc-memory', surface: 'terminal', ...BAND })
    if (await ui.find({ type: 'Text', text })) return ui
    await ui.unmount()
  }
  throw new Error('nothing was held')
}

const write = (id: string, file_path = PATH) => ({ tool: 'Write', tool_use_id: id, file_path, content: NOTE }) as any

describe('pc-memory write pane', () => {
  test('holds a note in the default memory directory, nothing else', () => {
    expect(isMemoryNote(PATH)).toBe(true)
    expect(isMemoryNote(`${DIR}/sub/MEMORY.md`)).toBe(true)
    expect(isMemoryNote(`${DIR}/MEMORY.md`)).toBe(false)
    expect(isMemoryNote('/repo/docs/x.md')).toBe(false)
    expect(isMemoryNote('/repo/projects/foo/memory/a.md')).toBe(false)
  })

  test('does not hold a path that climbs out of the memory directory', () => {
    expect(isMemoryNote(`${DIR}/../../../../.zshrc`)).toBe(false)
    expect(isMemoryNote(`${DIR}/./x.md`)).toBe(false)
    expect(isMemoryNote(`${DIR}//x.md`)).toBe(false)
  })

  test('reads the description, cleaned and cut', () => {
    expect(descriptionOf(NOTE)).toBe('Une note sur le niveau workspace')
    expect(descriptionOf('---\ndescription: "Avec: deux points"\n---\n')).toBe('Avec: deux points')
    expect(descriptionOf(`${String.fromCharCode(0xfeff)}---\r\ndescription: Fin Windows\r\n---\r\n`)).toBe('Fin Windows')
    expect(descriptionOf('---\ndescription: >-\n  texte\n---\n')).toBe('(no description)')
    expect(descriptionOf('---\ndescription:\nmetadata:\n---\n')).toBe('(no description)')
    expect(descriptionOf('sans frontmatter')).toBe('(no description)')
    expect(descriptionOf(`---\ndescription: \u001b[31m${'a'.repeat(300)}\n---\n`)).toBe('a'.repeat(200))
  })

  test('strips bidi and format characters', () => {
    expect(clean('a\u202eb\u200bc\nd')).toBe('a b c d')
  })

  test('✔︎ writes the note where it was aimed, after showing its description', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    const call = $.tool.call(write('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ type: 'Text', text: 'x.md · 7 l.' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Une note sur le niveau workspace' })).toBeDefined()
    expect(ran).toEqual([])
    await ui.press({ key: 'here' })
    await call
    expect(ran).toEqual([PATH])
    await ui.unmount()
  })

  test('Refuse blocks the write and tells the model', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    const call = $.tool.call(write('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'refuse' })
    const r: any = await call
    expect(r.deny).toMatch(/The user refused this memory note/)
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('a second write waits for the first answer, then is held in its turn', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    const first = $.tool.call(write('t1', `${DIR}/a.md`))
    const second = $.tool.call(write('t2', `${DIR}/b.md`))
    const ui1 = await heldBand($, /a\.md/)
    await ui1.press({ key: 'here' })
    await first
    expect(ran).toEqual([`${DIR}/a.md`])
    await ui1.unmount()
    const ui2 = await heldBand($, /b\.md/)
    await ui2.press({ key: 'here' })
    await second
    expect(ran).toEqual([`${DIR}/a.md`, `${DIR}/b.md`])
    await ui2.unmount()
  })

  test('a write outside memory is never held', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    await $.tool.call(write('t1', '/repo/docs/x.md'))
    expect(ran).toEqual(['/repo/docs/x.md'])
  })

  test('the permission check lifts only the ask of an accepted write', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    const call = $.tool.call(write('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'here' })
    await call
    const check = (id: string, file_path: string, content = NOTE): any =>
      $.tool.check({ tool: 'Write', input: { file_path, content }, tool_use_id: id } as any)
    expect((await check('t1', PATH)).decision).toBe('allow')
    expect((await check('t9', PATH)).decision).toBe('ask') // another call
    expect((await check('t1', PATH, `${NOTE} changed`)).decision).toBe('ask') // not what the person saw
    expect((await check('t1', `${DIR}/other.md`)).decision).toBe('ask') // not the path the person saw
    expect((await check('t1', `${DIR}/deny.md`)).decision).toBe('deny')
    await ui.unmount()
  })

  test('an ask carrying a settings rule is not held, the native prompt handles it', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    await $.tool.call(write('t1', `${DIR}/rule.md`))
    expect(ran).toEqual([`${DIR}/rule.md`])
  })

  test('a session with no surface that draws is not held', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, { surfaces: [] })
    await $.tool.call(write('t1'))
    expect(ran).toEqual([PATH])
  })

  test('a failing pause stops the wait instead of spinning', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, { sleepExit: 1 })
    await $.tool.call(write('t1'))
    expect(ran).toEqual([PATH])
  })

  test('a write the engine denies by itself is not held', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    await $.tool.call(write('t1', `${DIR}/deny.md`))
    expect(ran).toEqual([`${DIR}/deny.md`])
  })

  test('a write the engine allows is still held: the guard asks beneath it (auto mode)', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    const call = $.tool.call(write('t1', `${DIR}/allow.md`))
    const ui = await heldBand($)
    expect(ran).toEqual([])
    await ui.press({ key: 'here' })
    await call
    expect(ran).toEqual([`${DIR}/allow.md`])
    await ui.unmount()
  })

  test('PC_ALLOW_MEMORY_WRITE=1 skips the band', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, { env: '1' })
    await $.tool.call(write('t1'))
    expect(ran).toEqual([PATH])
  })

  test('an unanswered hold expires and falls back to the native prompt, remembering nothing', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    const before = holdLimits.ms
    holdLimits.ms = 40
    try {
      await $.tool.call(write('t1'))
    } finally {
      holdLimits.ms = before
    }
    expect(ran).toEqual([PATH])
    const out: any = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content: NOTE }, tool_use_id: 't1' } as any)
    expect(out.decision).toBe('ask')
  })

  test('⎘ writes the note at the level picked, and tells the model to index it there', async ($, on) => {
    const ran: string[] = []
    engine(on, ran)
    const call = $.tool.call(write('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ key: 'level-1' })).toBeDefined()
    await ui.press({ key: 'level-1' })
    const r: any = await call
    expect(ran).toEqual([`${PARENT}/x.md`])
    expect(r.context).toEqual([
      `The user moved this memory note to the pinky level: it was written at ${PARENT}/x.md, not at ${PATH}. It is already listed in that level's MEMORY.md. If you added a line for it to the MEMORY.md of the level first aimed at, remove that line.`,
    ])
    const check: any = await $.tool.check({ tool: 'Write', input: { file_path: `${PARENT}/x.md`, content: NOTE }, tool_use_id: 't1' } as any)
    expect(check.decision).toBe('allow')
    await ui.unmount()
  })

  test('⎘ falls back to asking the model to index when index_note.py fails', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, { indexExit: 1 })
    const call = $.tool.call(write('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'level-1' })
    const r: any = await call
    expect(r.context).toEqual([
      `The user moved this memory note to the pinky level: it was written at ${PARENT}/x.md, not at ${PATH}. Update that level's MEMORY.md index, not the one of the level first aimed at.`,
    ])
    await ui.unmount()
  })

  test('without levels (levels.py failed) the band offers only ✔︎ and ✘', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, { levels: 'not json' })
    const call = $.tool.call(write('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ key: 'level-1' })).toBeUndefined()
    expect(await ui.find({ key: 'here' })).toBeDefined()
    await ui.press({ key: 'here' })
    await call
    expect(ran).toEqual([PATH])
    await ui.unmount()
  })

  test('parses the levels, aimed level first, and rejects what it cannot trust', () => {
    const json = (levels: unknown, relative: unknown = 'x.md') => JSON.stringify({ levels, relative })
    const reversed = [LEVELS[1], LEVELS[0]]
    expect(parseLevels(json(reversed))).toEqual({ levels: [LEVELS[0], LEVELS[1]], relative: 'x.md' })
    expect(parseLevels(json(LEVELS, '../x.md'))).toEqual({ levels: [], relative: null })
    expect(parseLevels(json(LEVELS.map(l => ({ ...l, target: false }))))).toEqual({ levels: [], relative: null })
    expect(parseLevels(json(LEVELS, null))).toEqual({ levels: [], relative: null })
    expect(parseLevels('')).toEqual({ levels: [], relative: null })
  })
})
