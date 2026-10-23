import { describe, expect, test } from 'claude-code/testing'

import { clean, holdLimits, parseVerdict } from '../hooks/pii-hold/register'

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: true, maxRows: 12, bodyColumns: 120 } }
const PATH = '/repo/data/clients.csv'
const COPY = '/repo/data/clients.anon.csv'
const VERDICT = { flagged: true, kind: 'pii', path: PATH, columns: ['contact', 'name'], anonymize: true }

// Stands for the engine beneath the mod: the read runs, the permission check lets it through, the guard script answers.
type Engine = { verdict?: unknown; verdictExit?: number; copy?: unknown; copyExit?: number; surfaces?: string[]; sleepExit?: number; rule?: boolean; deny?: boolean; ask?: boolean }
function engine(on: any, ran: string[], calls: any[], opts: Engine = {}) {
  on('tool.call', (_$: any, e: any) => {
    ran.push(e.file_path)
    return { result: {}, text: 'ok' }
  })
  on('tool.check', () => {
    if (opts.deny) return { decision: 'deny', reason: 'engine' }
    // Par défaut le moteur laisse passer : $.tool.call applique le verdict de tool.check avant les hooks tool.call
    return opts.rule ? { decision: 'ask', reason: 'rule', rule: 'Read(*)' } : { decision: opts.ask ? 'ask' : 'allow', reason: 'guard' }
  })
  on('process.run', async (_$: any, e: any) => {
    calls.push(e)
    if (e.argv[0] === 'uv' && e.argv.includes('--verdict')) {
      return { value: { exitCode: opts.verdictExit ?? 0, stdout: JSON.stringify(opts.verdict ?? VERDICT), stderr: '' } }
    }
    if (e.argv[0] === 'uv' && e.argv.includes('--anonymize')) {
      return { value: { exitCode: opts.copyExit ?? 0, stdout: JSON.stringify(opts.copy ?? { copy: COPY }), stderr: '' } }
    }
    if (e.argv[0] === 'sleep') await new Promise(done => (globalThis as any).setTimeout(done, 5)) // a real wait, so the hold loop yields
    return { value: { exitCode: e.argv[0] === 'sleep' ? (opts.sleepExit ?? 0) : 0, stdout: '', stderr: '' } }
  })
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  let now = 0
  on('clock.now', () => ({ value: (now += 1000) })) // a clock that moves by itself, so a hold can expire
  on('ui.render', ($: any, e: any) => { const { Text } = $.ui.resolve(e); return <Text>{''}</Text> }) // la bande vide : rien sous le mod
}

async function heldBand($: any, text: RegExp | string = 'clients.csv') {
  for (let i = 0; i < 50; i++) {
    const ui = await $.ui.mount({ plugin: 'pc-privacy', surface: 'terminal', ...BAND })
    if (await ui.find({ type: 'Text', text })) return ui
    await ui.unmount()
  }
  throw new Error('nothing was held')
}

const read = (id: string, file_path = PATH) => ({ tool: 'Read', tool_use_id: id, file_path }) as any
const check = ($: any, id: string, file_path: string): any => $.tool.check({ tool: 'Read', input: { file_path }, tool_use_id: id } as any)

describe('pc-privacy read band', () => {
  test('publishes its folder for the other plugins at session start', async ($, on) => {
    const written: any[] = []
    on('state.set', ($: any, e: any, next: any) => {
      if (e.plugin !== 'pc-privacy' || e.key !== 'root') return next(e)
      written.push(e)
      return { value: { isSet: true, version: 1 } }
    })
    on('session.start', (_$: any, e: any) => ({ cwd: e.cwd })) // the engine beneath: nothing else to start
    await $.session.start({ cwd: '/repo' } as any)
    expect(written.map(w => [w.plugin, w.key])).toEqual([['pc-privacy', 'root']])
    expect(written[0].value).toMatch(/\/pc-privacy$/)
  })

  test('shows the file and the sensitive columns, and holds the read until the person answers', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ type: 'Text', text: 'contact, name' })).toBeDefined()
    expect(ran).toEqual([])
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('anonymize reads the copy, not the original, and tells the model', async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls)
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'anonymize' })
    const r: any = await call
    expect(ran).toEqual([COPY])
    expect(calls.find(c => c.argv.includes('--anonymize')).argv.slice(-2)).toEqual(['--anonymize', PATH])
    expect(r.context).toEqual([
      `The user chose an anonymized copy: you read ${COPY}, not ${PATH}. Its personal columns are replaced by pseudonyms; say so when you quote it.`,
    ])
    expect((await check($, 't1', COPY)).decision).toBe('allow') // the guard's ask on the copy is lifted too
    await ui.unmount()
  })

  test('anonymize that makes no copy refuses the read instead of falling back to the original', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { copyExit: 1 })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'anonymize' })
    const r: any = await call
    expect(r.deny).toBe(`The user asked for an anonymized copy of ${PATH} but none could be made. Tell them and ask what to do instead.`)
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('allow read reads the original and lifts the guard ask for that exact call, once', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { ask: true })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'allow' })
    const r: any = await call
    expect(ran).toEqual([PATH])
    expect(r.context).toBeUndefined()
    expect((await check($, 't1', PATH)).decision).toBe('allow')
    expect((await check($, 't9', PATH)).decision).toBe('ask') // another call
    expect((await check($, 't1', '/repo/data/other.csv')).decision).toBe('ask') // not the path the person saw
    await ui.unmount()
  })

  test('the first press wins: a second press cannot loosen the answer', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { ask: true })
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

  test('allow read is bound to the path the engine will see, as the model spelled it', async ($, on) => {
    const raw = '/repo/data//clients.csv'
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1', raw))
    const ui = await heldBand($)
    await ui.press({ key: 'allow' })
    await call
    expect((await check($, 't1', raw)).decision).toBe('allow')
    await ui.unmount()
  })

  test('deny blocks the read and points the model at anonymize_apply', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'deny' })
    const r: any = await call
    expect(r.deny).toBe(
      `The user refused to let you read ${PATH}: it holds personal data. Ask what to do instead; an anonymized copy can be made with \`anonymize_apply\` from \`pc-privacy\`.`,
    )
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('an oversize file offers no copy, only allow and deny', async ($, on) => {
    engine(on, [], [], { verdict: { ...VERDICT, kind: 'oversize', columns: [], anonymize: false } })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ type: 'Text', text: 'too large to analyze, may hold personal data' })).toBeDefined()
    expect(await ui.find({ key: 'anonymize' })).toBeUndefined()
    expect(await ui.find({ key: 'allow' })).toBeDefined()
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('a file the guard does not flag is not held', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { verdict: { flagged: false } })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
  })

  test('a guard script that fails leaves the read to the guard', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { verdictExit: 1 })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
  })

  test('the scan is given a long timeout, the cold start of the analysis being slow', async ($, on) => {
    const calls: any[] = []
    engine(on, [], calls, { verdict: { flagged: false } })
    await $.tool.call(read('t1'))
    expect(calls.find(c => c.argv.includes('--verdict')).init.timeoutMs).toBe(55_000)
  })

  test('a read the engine denies, or a settings rule decides, is not held', async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls, { deny: true })
    await $.tool.call(read('t1'))
    expect(calls).toEqual([])
  })

  test('a session with no surface that draws is not held', async ($, on) => {
    const ran: string[] = []
    const calls: any[] = []
    engine(on, ran, calls, { surfaces: [] })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
    expect(calls).toEqual([])
  })

  test('a session drawn only in VS Code is not held: the band is not raised there', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { surfaces: ['vscode'] })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
  })

  test('an unanswered hold expires and falls back to the native prompt, remembering nothing', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { ask: true })
    const before = holdLimits.ms
    holdLimits.ms = 40
    try {
      await $.tool.call(read('t1'))
    } finally {
      holdLimits.ms = before
    }
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
    const first = $.tool.call(read('t1', '/repo/a.csv'))
    const second = $.tool.call(read('t2', '/repo/b.csv'))
    const ui1 = await heldBand($)
    await ui1.press({ key: 'allow' })
    await first
    await ui1.unmount()
    const ui2 = await heldBand($)
    await ui2.press({ key: 'allow' })
    await second
    expect(ran).toEqual(['/repo/a.csv', '/repo/b.csv'])
    await ui2.unmount()
  })

  test('parses the verdict, and rejects what it cannot trust', () => {
    expect(parseVerdict(JSON.stringify(VERDICT))).toEqual({ path: PATH, kind: 'pii', columns: ['contact', 'name'], anonymize: true })
    expect(parseVerdict(JSON.stringify({ flagged: false }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ ...VERDICT, kind: 'other' }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ ...VERDICT, columns: [1] }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ ...VERDICT, anonymize: 'yes' }))).toBeNull()
    expect(parseVerdict('')).toBeNull()
  })

  test('cleans the column names it shows', () => {
    expect(parseVerdict(JSON.stringify({ ...VERDICT, columns: ['a b'] }))?.columns).toEqual(['a b'])
    expect(clean('x\u200by\nz')).toBe('x y z')
  })
})
