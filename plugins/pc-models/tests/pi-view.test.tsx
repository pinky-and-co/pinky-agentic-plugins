import { describe, expect, mock, test } from 'claude-code/testing'

import { fitViews, lingerLimits } from '../hooks/pi-view/register'

const ADV = 'mcp__plugin_pc-models_pc-models__adversarial'
const RUNS = '/cfg/pinky/plugin-state/pc-models/pi-runs'

// Stands for the engine beneath the mod: the tool call waits until the test lets it end, the view files are a map.
function engine(on: any, files: Record<string, string>, opts: { ownClock?: boolean } = {}) {
  const ran: any[] = []
  const rendered: any[] = []
  let finish: () => void = () => {}
  const ended = new Promise<void>(done => (finish = done))
  on('tool.call', async (_$: any, e: any) => {
    ran.push(e)
    await ended
    return { result: {}, text: 'ok' }
  })
  on('env.get', (_$: any, e: any) => ({ value: e.name === 'CLAUDE_CONFIG_DIR' ? '/cfg' : undefined }))
  on('fs.list', (_$: any, e: any) => ({
    value: Object.keys(files)
      .filter(path => path.startsWith(`${e.path}/`))
      .map(path => ({ name: path.slice(e.path.length + 1), kind: 'file', size: 1, mtime: 0 })),
  }))
  on('fs.read', (_$: any, e: any) => {
    if (e.path in files) return { value: files[e.path] }
    throw new Error('missing')
  })
  on('process.run', async () => {
    await new Promise(done => (globalThis as any).setTimeout(done, 5))
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  let now = 0
  if (!opts.ownClock) on('clock.now', () => ({ value: (now += 1000) }))
  on('ui.render', ($: any, e: any) => {
    rendered.push(e)
    const { Text } = $.ui.resolve(e)
    return <Text>{'engine row'}</Text>
  })
  return { ran, rendered, finish }
}

const call = (id: string, extra: object = {}) => ({ tool: ADV, tool_use_id: id, task: 'review', ...extra }) as any
const row = (id: string, isRunning = true) => ({
  component: 'ToolUse',
  props: { tool_use_id: id, tool: ADV, input: {}, isRunning, isErrored: false, isInterrupted: false },
})

describe('pi view', () => {
  test('the call leaves with its own run directory, whatever events the model wrote', async ($, on) => {
    const { ran, finish } = engine(on, {})
    finish()
    await $.tool.call(call('toolu_1', { events: '/etc/elsewhere' }))
    expect(ran.map(e => e.events)).toEqual([`${RUNS}/toolu_1`])
  })

  test('an id outside the pattern drops events', async ($, on) => {
    const { ran, finish } = engine(on, {})
    finish()
    await $.tool.call(call('../x', { events: '/etc/elsewhere' }))
    expect(ran.map(e => e.events)).toEqual([undefined])
  })

  test('the running row draws the title and every view of the run', async ($, on) => {
    const { finish } = engine(on, {
      [`${RUNS}/toolu_2/view-1.txt`]: 'slot 1 · glm\n[  0.5s] thinking · 40 chars\n',
      [`${RUNS}/toolu_2/view-2.txt`]: 'slot 2 · mistral\n[  0.2s] text: fine\n',
    })
    const running = $.tool.call(call('toolu_2'))
    let ui: any
    for (let i = 0; i < 50 && !ui; i++) {
      const mounted = await $.ui.mount({ plugin: 'pc-models', surface: 'terminal', ...row('toolu_2') } as any)
      if (await mounted.find({ type: 'Text', text: /pi ·/ })) ui = mounted
      else await mounted.unmount()
    }
    expect(ui).toBeDefined()
    for (const text of ['  ⎿ slot 1 · glm', '     [  0.5s] thinking · 40 chars', '  ⎿ slot 2 · mistral', '     [  0.2s] text: fine']) {
      expect(await ui.find({ type: 'Text', text })).toBeDefined()
    }
    await ui.unmount()
    finish()
    await running
  })

  test('an ended call keeps its view a few seconds, then the engine row comes back', async ($, on) => {
    const clock = mock.clock(on)
    const { finish } = engine(on, { [`${RUNS}/toolu_4/view-1.txt`]: '1 candidate(s)\n[  0.1s] text: quick\n[  0.2s] pool end\n' }, { ownClock: true })
    finish()
    await $.tool.call(call('toolu_4'))
    const kept = await $.ui.mount({ plugin: 'pc-models', surface: 'terminal', ...row('toolu_4', false) } as any)
    expect(await kept.find({ type: 'Text', text: /pi · done in \d+s/ })).toBeDefined()
    expect(await kept.find({ type: 'Text', text: '     [  0.1s] text: quick' })).toBeDefined()
    await kept.unmount()
    await clock.advance(lingerLimits.ms)
    const back = await $.ui.mount({ plugin: 'pc-models', surface: 'terminal', ...row('toolu_4', false) } as any)
    expect(await back.find({ type: 'Text', text: 'engine row' })).toBeDefined()
    await back.unmount()
  })

  test('a call that returns before the run is over keeps drawing the lines written after it, up to pool end', async ($, on) => {
    const path = `${RUNS}/toolu_5/view-1.txt`
    const files: Record<string, string> = { [path]: '1 candidate(s)\n[  1.0s] text: early\n' }
    const { finish } = engine(on, files)
    finish()
    await $.tool.call(call('toolu_5'))
    files[path] += '[130.0s] text: late\n[131.0s] pool end\n'
    let late: any
    for (let i = 0; i < 50 && !late; i++) {
      const mounted = await $.ui.mount({ plugin: 'pc-models', surface: 'terminal', ...row('toolu_5', false) } as any)
      if (await mounted.find({ type: 'Text', text: '     [131.0s] pool end' })) late = mounted
      else await mounted.unmount()
    }
    expect(late).toBeDefined()
    await late.unmount()
  })

  test('a finished row is the engine own', async ($, on) => {
    const { finish } = engine(on, {})
    finish()
    const ui = await $.ui.mount({ plugin: 'pc-models', surface: 'terminal', ...row('toolu_3', false) } as any)
    expect(await ui.find({ type: 'Text', text: 'engine row' })).toBeDefined()
    await ui.unmount()
  })

  test('a collapsed group holding a running call of ours is unfolded, another is not', async ($, on) => {
    const { rendered } = engine(on, {})
    const group = (tool: string) => ({
      component: 'ToolGroup',
      props: {
        calls: [{ tool_use_id: 't', tool, input: {}, isRunning: true, isErrored: false, isInterrupted: false }],
        isActive: true,
        isExpanded: false,
      },
    })
    for (const tool of [ADV, 'Read']) {
      const ui = await $.ui.mount({ plugin: 'pc-models', surface: 'terminal', ...group(tool) } as any)
      await ui.unmount()
    }
    expect(rendered.filter(e => e.component === 'ToolGroup').map(e => e.props.isExpanded)).toEqual([true, false])
  })

  test('the rows are shared between the views, each keeping its header', () => {
    const views = [1, 2, 3].map(n => [`slot ${n}`, ...Array.from({ length: 10 }, (_, i) => `${n}.${i}`)])
    expect(fitViews(views)).toEqual([
      ['slot 1', '1.7', '1.8', '1.9'],
      ['slot 2', '2.7', '2.8', '2.9'],
      ['slot 3', '3.7', '3.8', '3.9'],
    ])
  })

  test('the latest attempt line stays under the header once the stream scrolls past it', () => {
    const lines = Array.from({ length: 20 }, (_, i) => `[ ${i}.0s] text ${i}`)
    const view = [
      '18 candidate(s)',
      '[  0.0s] == model-a (attempt 1) ==',
      ...lines.slice(0, 5),
      '[  5.0s] == model-b (attempt 1) ==',
      ...lines.slice(5),
    ]
    expect(fitViews([view])).toEqual([
      ['18 candidate(s)', '[  5.0s] == model-b (attempt 1) ==', ...lines.slice(-10)],
    ])
  })
})
