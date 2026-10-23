import { describe, expect, test } from 'claude-code/testing'

import { clean, holdLimits, parseVerdict } from '../hooks/secrets-hold/register'

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: true, maxRows: 12, bodyColumns: 120 } }
const PATH = '/repo/config/creds.env'
const DIGEST = 'a'.repeat(64)
const VERDICT = { flagged: true, path: PATH, lines: [2, 5], digest: DIGEST, pragma: true, offline: true }

// Stands for the engine beneath the mod: the read runs, the guard script answers, the pause sleeps.
type Engine = { deny?: boolean; rule?: boolean; verdict?: unknown; verdictExit?: number; surfaces?: string[]; sleepExit?: number; pragmaOut?: string; pragmaExit?: number }
function engine(on: any, ran: string[], calls: string[][], opts: Engine = {}) {
  on('tool.call', (_$: any, e: any) => {
    ran.push(e.file_path)
    return { result: {}, text: 'ok' }
  })
  on('tool.check', () => {
    if (opts.deny) return { decision: 'deny', reason: 'engine' }
    // Par défaut le moteur laisse passer : $.tool.call applique le verdict de tool.check avant les hooks tool.call
    return opts.rule ? { decision: 'ask', reason: 'rule', rule: 'Read(*)' } : { decision: 'allow', reason: 'guard' }
  })
  on('process.run', async (_$: any, e: any) => {
    calls.push(e.argv)
    if (e.argv[0] === 'python3' && e.argv.includes('--verdict')) {
      return { value: { exitCode: opts.verdictExit ?? 0, stdout: JSON.stringify(opts.verdict ?? VERDICT), stderr: '' } }
    }
    if (e.argv[0] === 'python3' && e.argv.includes('--pragma')) {
      return { value: { exitCode: opts.pragmaExit ?? 0, stdout: opts.pragmaOut ?? '2\n', stderr: '' } }
    }
    if (e.argv[0] === 'sleep') await new Promise(done => (globalThis as any).setTimeout(done, 5)) // a real wait, so the hold loop yields
    return { value: { exitCode: e.argv[0] === 'sleep' ? (opts.sleepExit ?? 0) : 0, stdout: '', stderr: '' } }
  })
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  let now = 0
  on('clock.now', () => ({ value: (now += 1000) })) // a clock that moves by itself, so a hold can expire
  on('ui.render', ($: any, e: any) => { const { Text } = $.ui.resolve(e); return <Text>{''}</Text> }) // la bande vide : rien sous le mod
}

async function heldBand($: any, text: RegExp | string = PATH) {
  for (let i = 0; i < 50; i++) {
    const ui = await $.ui.mount({ plugin: 'pc-secrets', surface: 'terminal', ...BAND })
    if (await ui.find({ type: 'Text', text })) return ui
    await ui.unmount()
  }
  throw new Error('nothing was held')
}

const read = (id: string, file_path = PATH) => ({ tool: 'Read', tool_use_id: id, file_path }) as any

describe('pc-secrets read band', () => {
  test('shows the file and the flagged lines, and holds the read until the person answers', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ type: 'Text', text: 'line 2, 5' })).toBeDefined()
    expect(ran).toEqual([])
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('add pragma marks the flagged lines, then lets the read through and tells the model', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'pragma' })
    const r: any = await call
    expect(ran).toEqual([PATH])
    expect(r.context).toEqual([
      `The user marked 2 line(s) of ${PATH} as not secret (\`# pragma: allowlist secret\` added at the end of each) and allowed this read.`,
    ])
    await ui.unmount()
  })

  test('add pragma passes the path and every flagged line to the script', async ($, on) => {
    const ran: string[] = []
    const calls: string[][] = []
    engine(on, ran, calls)
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'pragma' })
    await call
    const argv = calls.find(a => a.includes('--pragma')) as string[]
    expect(argv.slice(1)).toEqual([argv[1], '--pragma', PATH, DIGEST, '2', '5'])
    await ui.unmount()
  })

  test('add pragma that marks nothing still lets the guard judge, without a claim to the model', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { pragmaOut: '0\n' })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'pragma' })
    const r: any = await call
    expect(ran).toEqual([PATH])
    expect(r.context).toBeUndefined()
    await ui.unmount()
  })

  test('delegate offline refuses the read and points the model at the offline tool', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'offline' })
    const r: any = await call
    expect(r.deny).toBe(
      `The user chose not to send ${PATH} to the cloud model, which holds secret-shaped data. Do not read it. Run the task locally with the \`offline\` tool of pc-models (files: ["${PATH}"]).`,
    )
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('deny blocks the read and tells the model', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    await ui.press({ key: 'deny' })
    const r: any = await call
    expect(r.deny).toBe(`The user refused to let you read ${PATH}: it holds secret-shaped data. Ask what to do instead.`)
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('delegate offline is offered only when the local pool answers', async ($, on) => {
    engine(on, [], [], { verdict: { ...VERDICT, offline: false } })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ key: 'offline' })).toBeUndefined()
    expect(await ui.find({ key: 'pragma' })).toBeDefined()
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  test('add pragma is not offered for a format a # comment would break', async ($, on) => {
    engine(on, [], [], { verdict: { ...VERDICT, pragma: false } })
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    expect(await ui.find({ key: 'pragma' })).toBeUndefined()
    expect(await ui.find({ key: 'offline' })).toBeDefined()
    await ui.press({ key: 'deny' })
    await call
    await ui.unmount()
  })

  for (const [why, opts] of [['the engine denies', { deny: true }], ['a settings rule decides', { rule: true }]] as const) {
    test(`a read ${why} is not held and nothing is written`, async ($, on) => {
      const ran: string[] = []
      const calls: string[][] = []
      engine(on, ran, calls, opts)
      await $.tool.call(read('t1'))
      expect(calls).toEqual([])
    })
  }

  test('the first press wins: a second press cannot loosen the answer', async ($, on) => {
    const ran: string[] = []
    const calls: string[][] = []
    engine(on, ran, calls)
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($)
    // Les deux appuis partent ensemble : la bande disparaît dès la première réponse, le second peut ne plus trouver son bouton.
    await Promise.allSettled([ui.press({ key: 'deny' }), ui.press({ key: 'pragma' })])
    const r: any = await call
    expect(r.deny).toMatch(/refused to let you read/)
    expect(calls.some(a => a.includes('--pragma'))).toBe(false)
    expect(ran).toEqual([])
    await ui.unmount()
  })

  test('shows the full path, not only the file name', async ($, on) => {
    engine(on, [], [])
    const call = $.tool.call(read('t1'))
    const ui = await heldBand($, PATH)
    expect(await ui.find({ type: 'Text', text: PATH })).toBeDefined()
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

  test('a session with no surface that draws is not held', async ($, on) => {
    const ran: string[] = []
    const calls: string[][] = []
    engine(on, ran, calls, { surfaces: [] })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
    expect(calls).toEqual([]) // the guard script is not even asked
  })

  test('a session drawn only in VS Code is not held: the band is not raised there', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [], { surfaces: ['vscode'] })
    await $.tool.call(read('t1'))
    expect(ran).toEqual([PATH])
  })

  test('an unanswered hold expires and falls back to the guard', async ($, on) => {
    const ran: string[] = []
    engine(on, ran, [])
    const before = holdLimits.ms
    holdLimits.ms = 40
    try {
      await $.tool.call(read('t1'))
    } finally {
      holdLimits.ms = before
    }
    expect(ran).toEqual([PATH]) // the next() beneath is the guard's own deny in a real session
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
    const first = $.tool.call(read('t1', '/repo/a.env'))
    const second = $.tool.call(read('t2', '/repo/b.env'))
    const ui1 = await heldBand($, PATH)
    await ui1.press({ key: 'pragma' })
    await first
    await ui1.unmount()
    const ui2 = await heldBand($, PATH)
    await ui2.press({ key: 'pragma' })
    await second
    expect(ran).toEqual(['/repo/a.env', '/repo/b.env'])
    await ui2.unmount()
  })

  test('parses the verdict, and rejects what it cannot trust', () => {
    expect(parseVerdict(JSON.stringify(VERDICT))).toEqual({ path: PATH, lines: [2, 5], digest: DIGEST, pragma: true, offline: true })
    expect(parseVerdict(JSON.stringify({ ...VERDICT, digest: 1 }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ flagged: false }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ ...VERDICT, lines: [] }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ ...VERDICT, lines: ['2'] }))).toBeNull()
    expect(parseVerdict(JSON.stringify({ ...VERDICT, pragma: 'yes' }))).toBeNull()
    expect(parseVerdict('')).toBeNull()
  })

  test('strips bidi and format characters', () => {
    expect(clean('a\u202eb\u200bc\nd')).toBe('a b c d')
  })
})
