// Draws a running `adversarial`, `panel` or `offline` call like a subagent: the `pi` run it started, live, in the call's own
// row. The call leaves with `events` naming its run directory; the server writes there one `view-<slot>.txt` per `pi`
// stream (see `mcp/pi_live.py`), and this mod reads them back until the call ends.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { PiRun } from '../../types'

export const TOOL = /^mcp__plugin_pc-models_pc-models__(adversarial|panel|offline)$/
const ID = /^[A-Za-z0-9_-]{1,128}$/
const VIEW = /^view-(\d+)\.txt$/
// Lines drawn under the title, shared between the streams of a panel.
const ROWS = 12
// How long the view stays in the row once the call has ended, so a quick answer can still be read. Exported for the tests.
export const lingerLimits = { ms: 4_000, stallMs: 1_800_000, noViewMs: 10_000 }
// The server's closing line of a stream (`[<stamp>s] pool end`); a run is over once every stream has written it.
const POOL_END = /^\[ *\d+(?:\.\d+)?s\] pool end$/
const poolEnded = (views: string[][]) => views.length > 0 && views.every(v => v.some(line => POOL_END.test(line)))
const UNSAFE = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

const runs = atom({ plugin: 'pc-models', key: 'piRuns' } as const, {} as Record<string, PiRun>)

async function runsDir($: EngineInterface): Promise<string> {
  const config = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${await $.env.get('HOME')}/.claude`
  return `${config}/pinky/plugin-state/pc-models/pi-runs`
}

// The view files of a run, in slot order, each as its lines.
export async function readViews($: EngineInterface, dir: string): Promise<string[][]> {
  const entries = await $.fs.list(dir).catch(() => [])
  const slots = entries
    .map(f => ({ name: f.name, slot: Number(VIEW.exec(f.name)?.[1]) }))
    .filter(f => Number.isInteger(f.slot))
    .sort((a, b) => a.slot - b.slot)
  const views: string[][] = []
  for (const { name } of slots) {
    const text = await $.fs.read(`${dir}/${name}`).catch(() => '')
    views.push(String(text).split('\n').filter(Boolean).map(line => line.replace(UNSAFE, '?')))
  }
  return views
}

// The server's line opening an attempt on one pool entry (`[<stamp>s] == <entry> (attempt N) ==`).
const ATTEMPT = /^\[ *\d+(?:\.\d+)?s\] == .+ \(attempt \d+\) ==$/

// Each view's header and its last lines, the rows split evenly between the views; the latest attempt line stays pinned
// under the header once the tail has scrolled past it, so the model at work is always named.
export function fitViews(views: string[][]): string[][] {
  const per = Math.max(1, Math.floor(ROWS / Math.max(1, views.length)) - 1)
  return views.map(([header = '', ...lines]) => {
    const at = lines.findLastIndex(line => ATTEMPT.test(line))
    if (at < 0 || at >= lines.length - per) return [header, ...lines.slice(-per)]
    return [header, lines[at], ...lines.slice(-Math.max(1, per - 1))]
  })
}

async function pause($: EngineInterface): Promise<boolean> {
  try {
    return (await $.process.run(['sleep', '0.25'])).exitCode === 0
  } catch {
    return false
  }
}

export const register: Register = on => {
  on('tool.call', { tool: TOOL }, async ($, e, next) => {
    const id = (e as { tool_use_id?: unknown }).tool_use_id
    // The model never chooses where the server writes: its own `events` value is always replaced or dropped.
    if (typeof id !== 'string' || !ID.test(id)) return next({ ...e, events: undefined } as typeof e)
    const dir = `${await runsDir($)}/${id}`
    const startedAt = await $.clock.now()
    let returned = false
    let stop = false
    let loopDone = false
    let finished = false
    // A call past the harness's foreground limit hands control back while the server keeps writing: the view follows the
    // run until every stream has written its `pool end` line, not until the call returns.
    const finish = async () => {
      if (finished) return
      finished = true
      const views = await readViews($, dir)
      const endedAt = await $.clock.now()
      await update($, runs, all => (views.length > 0 ? { ...all, [id]: { startedAt, endedAt, views } } : all)).catch(() => undefined)
      // Le retrait passe par une minuterie : le résultat de l'appel n'attend pas la fin de l'affichage.
      $.clock.after(lingerLimits.ms, () => update($, runs, ({ [id]: _, ...rest }) => rest))
    }
    const poll = (async () => {
      let seen = ''
      let changedAt = startedAt
      let returnedAt: number | undefined
      try {
        while (!stop) {
          const views = await readViews($, dir)
          const now = await $.clock.now()
          const signature = JSON.stringify(views)
          if (signature !== seen) {
            [seen, changedAt] = [signature, now]
            if (views.length > 0) await update($, runs, all => ({ ...all, [id]: { startedAt, views } }))
          }
          if (returned) returnedAt ??= now
          // A call that returned without any view (refused, blocked) has nothing to follow.
          if (returnedAt !== undefined && views.length === 0 && now - returnedAt > lingerLimits.noViewMs) break
          if (returned && (poolEnded(views) || now - changedAt > lingerLimits.stallMs)) break
          if (!(await pause($))) break
        }
      } finally {
        loopDone = true
        if (returned) await finish()
      }
    })().catch(() => undefined)
    try {
      return await next({ ...e, events: dir } as typeof e)
    } finally {
      returned = true
      if (loopDone) await finish().catch(() => undefined)
      else if (poolEnded(await readViews($, dir))) {
        // A run that is over ends with the call; one still going (or not started) is followed by the loop above, and the
        // result of the call does not wait for it.
        stop = true
        await poll.catch(() => undefined)
      } else void poll.catch(() => undefined)
    }
  }).catch(($, e, next) => (next.called ? next(e) : next({ ...e, events: undefined } as typeof e)))

  // A collapsed group draws one line for all its calls: it is unfolded while one of ours runs or lingers, so that call's
  // row draws.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    const shown = await read($, runs)
    const ours = e.props.calls.some(c => TOOL.test(c.tool) && (c.isRunning || (c.tool_use_id !== undefined && c.tool_use_id in shown)))
    return ours ? next({ ...e, props: { ...e.props, isExpanded: true } }) : next(e)
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    if (!TOOL.test(e.props.tool)) return next(e)
    const run = (await read($, runs))[e.props.tool_use_id]
    if (!run) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const seconds = Math.max(0, Math.round(((run.endedAt ?? (await $.clock.now())) - run.startedAt) / 1000))
    const tool = TOOL.exec(e.props.tool)?.[1] ?? 'pi'
    const state = run.endedAt === undefined ? `${seconds}s` : `done in ${seconds}s`
    return (
      <Box flexDirection="column">
        <Text bold>{`⏺ ${tool} · 𝞹 pi · ${state}`}</Text>
        {fitViews(run.views).flatMap((view, v) =>
          view.map((line, i) => (
            <Text key={`v${v}-${i}`} dimColor={i > 0}>{`${i === 0 ? '  ⎿ ' : '     '}${line}`}</Text>
          )),
        )}
      </Box>
    )
  })
}
