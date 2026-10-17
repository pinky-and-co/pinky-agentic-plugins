import { describe, expect, test } from 'claude-code/testing'

import { clean, holdLimits, parseVerdict } from '../hooks/read-cost-hold/register'

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: true, maxRows: 12, bodyColumns: 120 } }
const PATH = '/repo/docs/report.pdf'
const MARKDOWN = '/tmp/pc-read-Ab12/report.md'
const VERDICT = { flagged: true, path: PATH, markitdown: true }
const PRIVACY_ROOT = '/plugins/pc-privacy'

// Stands for the engine beneath the mod: the read runs, the permission check asks, the hook script and markitdown answer.
type Engine = { verdict?: unknown; verdictExit?: number; targetExit?: number; markitdownExit?: number; markitdownTimesOut?: boolean; schemaExit?: number; surfaces?: string[]; sleepExit?: number; rule?: boolean; ruleOnAccepted?: boolean; deny?: boolean; privacy?: { published?: boolean | 'by a plugin'; stdout?: string; exit?: number; throws?: boolean; stateThrows?: boolean } }
function engine(on: any, ran: string[], calls: any[], opts: Engine = {}) {
  on('tool.call', (_$: any, e: any) => {
    ran.push(e.file_path)
    return { result: {}, text: 'ok' }
  })
  on('tool.check', (_$: any, e: any) => {
    if (opts.deny) return { decision: 'deny', reason: 'engine' }
    // `ruleOnAccepted` : le contrôle du mod (sans `tool_use_id`) ne voit pas de règle, celui qui porte un `tool_use_id` en voit une.
    const ruled = opts.rule || (opts.ruleOnAccepted && e.tool_use_id !== undefined)
    return ruled ? { decision: 'ask', reason: 'rule', rule: 'Read(*)' } : { decision: 'ask', reason: 'guard' }
  })
  // pc-privacy publishes its folder at session start; a test that does not publish it leaves the value unset.
  on('state.get', (_$: any, e: any, next: any) => {
    if (opts.privacy?.stateThrows) throw new Error('state unavailable')
    if (e.plugin !== 'pc-privacy' || e.key !== 'root' || opts.privacy?.published === 'by a plugin') return next(e)
    return { value: opts.privacy?.published ? { value: PRIVACY_ROOT, version: 1 } : { value: undefined, version: 0 } }
  })
  on('process.run', async (_$: any, e: any) => {
    calls.push(e)
    if (e.argv.some((a: string) => a.endsWith('/pii_guard.py'))) {
      if (opts.privacy?.throws) throw new Error('cannot start')
      return { value: { exitCode: opts.privacy?.exit ?? 0, stdout: opts.privacy?.stdout ?? JSON.stringify({ flagged: true }), stderr: '' } }
    }
    if (e.argv[0] === 'python3' && e.argv.includes('--verdict')) {
      const asked = JSON.parse(e.init.stdin).tool_input.file_path // the verdict follows the file the mod asked about
      return { value: { exitCode: opts.verdictExit ?? 0, stdout: JSON.stringify(opts.verdict ?? { ...VERDICT, path: asked, data: asked.endsWith('.xlsx') }), stderr: '' } }
    }
    if (e.argv[0] === 'python3' && e.argv.includes('--target')) {
      return { value: { exitCode: opts.targetExit ?? 0, stdout: JSON.stringify({ markdown: MARKDOWN }), stderr: '' } }
    }
    if (e.argv.some((a: string) => a.endsWith('/data_schema.py'))) {
      return { value: { exitCode: opts.schemaExit ?? 0, stdout: '', stderr: '' } }
    }
    if (e.argv[0] === 'markitdown') {
      if (opts.markitdownTimesOut) throw new Error('timed out')
      return { value: { exitCode: opts.markitdownExit ?? 0, stdout: '', stderr: '' } }
    }
    if (e.argv[0] === 'sleep') await new Promise(done => (globalThis as any).setTimeout(done, 5)) // a real wait, so the hold loop yields
    return { value: { exitCode: e.argv[0] === 'sleep' ? (opts.sleepExit ?? 0) : 0, stdout: '', stderr: '' } }
  })
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  let now = 0
  on('clock.now', () => ({ value: (now += 1000) })) // a clock that moves by itself, so a hold can expire
  on('ui.render', ($: any, e: any) => { const { Text } = $.ui.resolve(e); return <Text>{''}</Text> }) // la bande vide : rien sous le mod
}

async function heldBand($: any, text: RegExp | string = 'report.pdf') {
  for (let i = 0; i < 50; i++) {
    const ui = await $.ui.mount({ plugin: 'pc-read', surface: 'terminal', ...BAND })
    if (await ui.find({ type: 'Text', text })) return ui
    await ui.unmount()
  }
  throw new Error('nothing was held')
}

const read = (id: string, file_path = PATH) => ({ tool: 'Read', tool_use_id: id, file_path }) as any
const check = ($: any, id: string, file_path: string): any => $.tool.check({ tool: 'Read', input: { file_path }, tool_use_id: id } as any)

describe('pc-read costly-read band', () => {
  test('read as-is lifts the hook ask for that exact call, once', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect((await check($, 't1', PATH)).decision).toBe('ask') // held: nothing lifted yet
    await ui.press({ key: 'allow' })
    await call
    expect(ran).toEqual([PATH])
    expect((await check($, 't1', PATH)).decision).toBe('allow')
    expect((await check($, 't9', PATH)).decision).toBe('ask') // the next call on the same file is held again
    expect((await check($, 't1', '/repo/docs/other.pdf')).decision).toBe('ask') // not the path the person saw
    await ui.unmount()
  })

  for (const name of ['report.pdf']) {
    test(`shows ${name} with convert, read as-is and deny, in that order`, async ($, on) => {
      const path = `/repo/docs/${name}`
      engine(on, [], [], { verdict: { ...VERDICT, path } })
      const call = $.tool.call(read('t1', path))
      const ui = await heldBand($, name)
      const buttons = await ui.findAll({ type: 'Button' })
      expect(buttons.map((b: any) => b.key)).toEqual(['convert', 'allow', 'deny'])
      expect(buttons.map((b: any) => b.props.label)).toEqual(['⎘ convert to Markdown', '✔︎ read as-is', '✘ deny'])
      await ui.press({ key: 'deny' })
      await call
      await ui.unmount()
    })
  }

  test('shows an .xlsx with the schema button too, and no other format does', async ($, on) => {
    const path = '/repo/docs/book.xlsx'
    engine(on, [], [], { verdict: { ...VERDICT, path, data: true } })
    const call = $.tool.call(read('t1', path))
    const ui = await heldBand($, 'book.xlsx')
    const buttons = await ui.findAll({ type: 'Button' })
    expect(buttons.map((b: any) => b.key)).toEqual(['convert', 'schema', 'allow', 'deny'])
    expect(buttons.map((b: any) => b.props.label)).toEqual(['⎘ convert to Markdown', '▤ schema + first rows', '✔︎ read as-is', '✘ deny'])
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('schema runs the data-schema script into the target folder, then refuses the read naming its output', async ($, on) => {
    const path = '/repo/docs/book.xlsx'
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls, { verdict: { ...VERDICT, path, data: true }, privacy: { published: true, stdout: JSON.stringify({ flagged: false }) } })
    const call = $.tool.call(read('t1', path))
    const ui = await heldBand($, 'book.xlsx')
    await ui.press({ key: 'schema' })
    const r: any = await call
    const script = calls.find(c => c.argv.some((a: string) => a.endsWith('/data_schema.py')))
    expect(script.argv.slice(0, 3)).toEqual(['uv', 'run', '--script'])
    expect(script.argv.slice(4)).toEqual(['--privacy-root', PRIVACY_ROOT, '--out', MARKDOWN, '--', path])
    expect(calls.some(c => c.argv.includes('--discard'))).toBe(false)
    expect(r.deny).toContain(`read ${MARKDOWN} instead`)
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('a failing schema script refuses the read without a path and removes the folder', async ($, on) => {
    const path = '/repo/docs/book.xlsx'
    const calls: any[] = []
    engine(on, [], calls, { verdict: { ...VERDICT, path, data: true }, schemaExit: 2 })
    const call = $.tool.call(read('t1', path))
    const ui = await heldBand($, 'book.xlsx')
    await ui.press({ key: 'schema' })
    const r: any = await call
    expect(r.deny).toBe(`The user asked for the schema and first rows of ${path} but it failed. Tell them and ask what to do instead.`)
    expect(calls.some(c => c.argv.includes('--discard'))).toBe(true)
    await ui.unmount()
  })

  test('holds the read until the person answers', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(ran).toEqual([])
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('convert runs markitdown into the target folder, then refuses the read naming the Markdown', async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls)
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'convert' })
    const r: any = await call
    const converter = calls.find(c => c.argv[0] === 'markitdown')
    expect(converter.argv).toEqual(['markitdown', '-o', MARKDOWN, '--', PATH])
    expect(calls.some(c => c.argv.includes('--discard'))).toBe(false) // a produced Markdown stays
    expect(r.deny).toBe(`The user chose to read ${PATH} as Markdown: read ${MARKDOWN} instead (converted with markitdown).`)
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('convert is given a long timeout, a heavy file being slow to convert', async ($, on) => {
    const calls: any[] = []
    engine(on, [], calls)
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'convert' })
    await call
    expect(calls.find(c => c.argv[0] === 'markitdown').init.timeoutMs).toBeGreaterThan(30_000)
    await ui.unmount()
  })

  for (const [why, opts, folder] of [
    ['markitdown fails', { markitdownExit: 1 }, true],
    ['markitdown times out', { markitdownTimesOut: true }, true],
    ['no target folder can be made', { targetExit: 1 }, false],
  ] as const) {
    test(`convert refuses the read without a Markdown path when ${why}${folder ? ' and removes the folder it made' : ''}`, async ($, on) => {
      const ran: string[] = []
      const calls: any[] = []
      engine(on, ran, calls, opts)
      const call = $.tool.call(read('t1'))
      const ui = await heldBand($)
      await ui.press({ key: 'convert' })
      const r: any = await call
      expect(r.deny).toBe(`The user asked to convert ${PATH} to Markdown but the conversion failed. Tell them and ask what to do instead.`)
      expect(ran).toEqual([])
      const discards = calls.filter(c => c.argv.includes('--discard')).map(c => c.argv.slice(1))
      expect(discards).toEqual(folder ? [[expect.stringMatching(/read_cost\.py$/), '--discard', MARKDOWN]] : [])
      await ui.unmount()
    })
  }

  test('read as-is passes the call through to the engine', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'allow' })
    const r: any = await call
    expect(r.deny).toBeUndefined()
    expect(ran).toEqual([PATH])
    await ui.unmount()
  })

  test('a settings rule keeps its ask even for the accepted call', async ($, on) => {
    engine(on, [], [], { ruleOnAccepted: true })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($) // no rule when the mod checks at call time: the read is held
    await ui.press({ key: 'allow' })
    await call
    // the check that carries the call id now sees a rule: the accepted call is not lifted
    expect((await check($, 't1', PATH)).decision).toBe('ask')
    await ui.unmount()
  })

  test('deny blocks the read and tells the model', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'deny' })
    const r: any = await call
    expect(r.deny).toBe(`The user refused to let you read ${PATH}. Ask what to do instead.`)
    expect(ran).toEqual([])
    expect((await check($, 't1', PATH)).decision).toBe('ask')
    await ui.unmount()
  })

  test('without markitdown the band offers read as-is and deny, and says how to install it', async ($, on) => {
    engine(on, [], [], { verdict: { ...VERDICT, markitdown: false } })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect((await ui.findAll({ type: 'Button' })).map((b: any) => b.key)).toEqual(['allow', 'deny'])
    expect(await ui.find({ type: 'Text', text: "markitdown is not installed: uv tool install 'markitdown[all]'" })).toBeDefined()
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('with markitdown the band does not talk about installing it', async ($, on) => {
    engine(on, [], [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ type: 'Text', text: /install/ })).toBeUndefined()
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  for (const [why, opts] of [['the engine denies', { deny: true }], ['a settings rule decides', { rule: true }]] as const) {
    test(`a read ${why} is not held and nothing is run`, async ($, on) => {
      const ran: string[] = []
      const calls: any[] = []
      engine(on, ran, calls, opts)
      await $.tool.call(read('t1'))
      expect(ran).toEqual([PATH])
      expect(calls).toEqual([])
    })
  }

  test('the first press wins: a second press cannot loosen the answer', async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls)
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    // Les deux appuis partent ensemble : la bande disparaît dès la première réponse, le second peut ne plus trouver son bouton.
    await Promise.allSettled([ui.press({ key: 'deny' }), ui.press({ key: 'allow' })])
    const r: any = await call
    expect(r.deny).toMatch(/refused to let you read/)
    expect(ran).toEqual([])
    expect((await check($, 't1', PATH)).decision).toBe('ask')
    await ui.unmount()
  })

  test('a file pc-privacy flags is not held: its own band decides, and nothing is lifted', async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls, { privacy: { published: true } })
    await $.tool.call(read('t1')) // returns without a press: no hold
    expect(ran).toEqual([PATH]) // next() was called
    const asked = calls.find(c => c.argv.some((a: string) => a.endsWith('/pii_guard.py')))
    expect(asked.argv).toEqual(['uv', 'run', '--script', `${PRIVACY_ROOT}/hooks/pii_guard.py`, '--verdict'])
    expect(JSON.parse(asked.init.stdin)).toEqual({ tool_name: 'Read', tool_input: { file_path: PATH } })
    expect(asked.init.timeoutMs).toBe(55_000)
    expect(calls.some(c => c.argv[0] === 'sleep')).toBe(false) // never entered the wait
    expect((await check($, 't1', PATH)).decision).toBe('ask') // tool.check unchanged
  })

  test('reads the folder pc-privacy wrote to the shared state itself, not a stand-in', {
    plugins: [{
      name: 'pc-privacy',
      register(on: any) {
        on('session.start', async ($: any, e: any, next: any) => {
          await $.state.set({ plugin: 'pc-privacy', key: 'root' } as const, '/plugins/pc-privacy')
          return next(e)
        })
      },
    }],
  }, async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls, { privacy: { published: 'by a plugin' } })
    on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
    await $.session.start({ cwd: '/repo' } as any)
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
    expect(calls.some(c => c.argv.includes(`${PRIVACY_ROOT}/hooks/pii_guard.py`))).toBe(true)
  })

  test('still sees the folder pc-privacy published after a /clear', {
    plugins: [{
      name: 'pc-privacy',
      register(on: any) {
        on('session.start', async ($: any, e: any, next: any) => {
          await $.state.set({ plugin: 'pc-privacy', key: 'root' } as const, '/plugins/pc-privacy')
          return next(e)
        })
      },
    }],
  }, async ($, on) => {
    const calls: any[] = []
    engine(on, [], calls, { privacy: { published: 'by a plugin' } })
    on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
    on('session.end', (_$: any, e: any) => ({ sessionId: e.sessionId }))
    await $.session.start({ cwd: '/repo' } as any)
    await $.session.end({ reason: 'clear', sessionId: 's1', resume: { id: 's1' } } as any) // no session.start follows a /clear
    await $.tool.call(read('t1'))
    expect(calls.some(c => c.argv.includes(`${PRIVACY_ROOT}/hooks/pii_guard.py`))).toBe(true)
  })

  test('a file pc-privacy does not flag is held as before', async ($, on) => {
    const calls: any[] = []
    engine(on, [], calls, { privacy: { published: true, stdout: JSON.stringify({ flagged: false }) } })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    // held because the guard answered, not because it was never asked
    const asked = calls.filter(c => c.argv.some((a: string) => a.endsWith('/pii_guard.py')))
    expect(asked.length).toBe(1)
    expect(JSON.parse(asked[0].init.stdin)).toEqual({ tool_name: 'Read', tool_input: { file_path: PATH } })
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('without pc-privacy (nothing published) the read is held and its guard is not run', async ($, on) => {
    const calls: any[] = []
    engine(on, [], calls)
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(calls.some(c => c.argv.some((a: string) => a.endsWith('/pii_guard.py')))).toBe(false)
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  for (const [why, privacy] of [
    ['exits non-zero', { published: true, exit: 1 }],
    ['times out or cannot start', { published: true, throws: true }],
    ['prints something else than JSON', { published: true, stdout: 'oops' }],
    ['answers without a verdict', { published: true, stdout: '{}' }],
  ] as const) {
    test(`the read is not held when the published pc-privacy guard ${why}: its dialog decides`, async ($, on) => {
      const ran: string[] = []
      const calls: any[] = []
      engine(on, ran, calls, { privacy })
      await $.tool.call(read('t1')) // returns without a press: no hold
      expect(ran).toEqual([PATH]) // next() was called
      expect(calls.some(c => c.argv[0] === 'sleep')).toBe(false) // never entered the wait
      expect((await check($, 't1', PATH)).decision).toBe('ask') // nothing remembered, tool.check unchanged
    })
  }

  test('a failing read of the shared state is treated as no pc-privacy: the read is held', async ($, on) => {
    const calls: any[] = []
    engine(on, [], calls, { privacy: { published: true, stateThrows: true } })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(calls.some(c => c.argv.some((a: string) => a.endsWith('/pii_guard.py')))).toBe(false)
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('a copy pc-privacy anonymized is not held, and nothing is asked of any guard', async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    const copy = '/repo/docs/book.anon.xlsx'
    engine(on, ran, calls, { privacy: { published: true } })
    await $.tool.call(read('t1', copy)) // returns without a press: no hold
    expect(ran).toEqual([copy])
    expect(calls).toEqual([])
  })

  test('a file the hook does not flag is not held', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { verdict: { flagged: false } })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
  })

  test('a hook script that fails leaves the read to the hook', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { verdictExit: 1 })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
  })

  for (const [why, surfaces] of [['no surface that draws', []], ['VS Code alone', ['vscode']]] as const) {
    test(`a session with ${why} is not held: the hook ask applies at once`, async ($, on) => {
      const ran: string[] = []
      const calls: any[] = []
      engine(on, ran, calls, { surfaces: [...surfaces] })
      await $.tool.call(read('t1'))
      expect(ran).toEqual([PATH])
      expect(calls).toEqual([]) // the hook script is not even asked
      expect((await check($, 't1', PATH)).decision).toBe('ask')
    })
  }

  test('an unanswered hold expires and falls back to the hook ask, remembering nothing', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const before = holdLimits.ms
    holdLimits.ms = 40
    try {
      await $.tool.call(read('t1'))
    } finally {
      holdLimits.ms = before
    }
    expect(ran).toEqual([PATH]) // the next() beneath is the hook's own ask in a real session
    expect((await check($, 't1', PATH)).decision).toBe('ask')
  })

  test('a failing pause stops the wait instead of spinning', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { sleepExit: 1 })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
  })

  test('a second read waits for the first answer, then is held in its turn', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const first = $.tool.call(read('t1', '/repo/a.pdf'))
    const second = $.tool.call(read('t2', '/repo/b.pdf'))
    const ui1 = await heldBand($, 'a.pdf')
    expect(await ui1.find({ type: 'Text', text: 'b.pdf' })).toBeUndefined() // b waits behind a
    expect(ran).toEqual([])
    await ui1.press({ key: 'allow' })
    await first
    await ui1.unmount()
    expect(ran).toEqual(['/repo/a.pdf']) // b has not run before a was answered
    const ui2 = await heldBand($, 'b.pdf')
    await ui2.press({ key: 'allow' })
    await second
    expect(ran).toEqual(['/repo/a.pdf', '/repo/b.pdf'])
    await ui2.unmount()
  })

  test('parses the verdict, and rejects what it cannot trust', () => {
    expect(parseVerdict(JSON.stringify(VERDICT))).toEqual({ path: PATH, convert: true, data: false })
    expect(parseVerdict(JSON.stringify({ ...VERDICT, markitdown: false }))).toEqual({ path: PATH, convert: false, data: false })
    expect(parseVerdict(JSON.stringify({ ...VERDICT, data: true }))).toEqual({ path: PATH, convert: true, data: true })
    expect(parseVerdict(JSON.stringify({ ...VERDICT, markitdown: 'yes' }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ flagged: false }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ ...VERDICT, path: 1 }))).toBeNull()
    expect(parseVerdict('')).toBeNull()
  })

  test('strips bidi and format characters', () => {
    expect(clean('a\u202eb\u200bc\nd')).toBe('a b c d')
  })
})
