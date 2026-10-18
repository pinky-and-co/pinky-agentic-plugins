// Routes each Agent call whose model the session chooses: a classifier ranks the task, the proposal shows in a band
// above the prompt, and the call leaves with the user's pick (or the proposal, unanswered or with no band drawn).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { HeldAgent, RoutedChoice } from '../../types'
import { tierMark } from './tiers'

const MODELS = ['haiku', 'sonnet', 'opus'] as const
const EFFORTS = ['low', 'medium', 'high'] as const
const CARD_WORKER = 'pc-kanban:card-worker'
const CARD_LINE = /^Card:[ \t]*(\/\S+\.md)[ \t]*$/m
const MAX_LENGTH = 80
const UNSAFE = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

// Past this long without a key, the call leaves with the proposal. Exported so the tests shorten it.
export const holdLimits = { ms: 60_000 }

const held = atom({ plugin: 'pc-core', key: 'held' } as const, null as HeldAgent | null)

type Proposal = RoutedChoice & { category: string; error?: string; classifierModel?: string }
type Exit = 'user-choice' | 'timeout' | 'no-band'
type AgentCall = { tool_use_id: string; prompt?: unknown; subagent_type?: unknown; model?: unknown }

const isModel = (v: unknown): v is (typeof MODELS)[number] => MODELS.some(m => m === v)
const isEffort = (v: unknown): v is (typeof EFFORTS)[number] => EFFORTS.some(f => f === v)

export function clean(text: string): string {
  return text.replace(UNSAFE, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_LENGTH)
}

// The `model:` and `effort:` lines of a card's frontmatter, when they name an allowed value.
export function cardChoice(card: string): { model?: string; effort?: string } {
  const head = /^---\r?\n([\s\S]*?)\r?\n---/.exec(card)?.[1] ?? ''
  const field = (name: string) => new RegExp(`^${name}:[ \\t]*(\\S+)[ \\t]*$`, 'm').exec(head)?.[1]
  const model = field('model')
  const effort = field('effort')
  return { ...(isModel(model) ? { model } : {}), ...(isEffort(effort) ? { effort } : {}) }
}

// The `model:` of the frontmatter of a definition file; `inherit` leaves the choice to the session, so it is no model.
export function ownModel(definition: string): string | null {
  const head = /^---\r?\n([\s\S]*?)\r?\n---/.exec(definition)?.[1] ?? ''
  const value = /^model:[ \t]*["']?([^"'\s]+)["']?[ \t]*$/m.exec(head)?.[1]
  return value === undefined || value === 'inherit' ? null : value
}

// The model an agent type carries in its definition file. A plugin type (`<plugin>:<name>`) reads `agents/<name>.md` of the
// plugin: first its working-tree sibling of this plugin (the directory-marketplace layout, where agents run from), then the
// install path of the registry `installed_plugins.json`. A bare type reads `.claude/agents/<type>.md` of the project, then of
// the config directory. The first file found answers; a built-in type, no file and an unreadable registry answer null.
async function definedModel($: EngineInterface, type: string): Promise<string | null> {
  if (/[/\\]|\.\./.test(type)) return null
  const readDefinition = async (path: string) => {
    try {
      return await $.fs.read(path)
    } catch {
      return null
    }
  }
  const found = (text: string | null) => (text === null ? null : { model: ownModel(text) })
  const configDir = async () => (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${await $.env.get('HOME')}/.claude`
  try {
    const parts = type.split(':')
    if (parts.length === 1) {
      const root = await $.session.root().catch(() => undefined)
      const project = root === undefined ? null : await readDefinition(`${root}/.claude/agents/${type}.md`)
      const hit = found(project) ?? found(await readDefinition(`${await configDir()}/agents/${type}.md`))
      return hit?.model ?? null
    }
    const [plugin, name, ...rest] = parts
    if (!plugin || !name || rest.length > 0) return null
    const sibling = found(await readDefinition(`${$.plugin.root}/../${plugin}/agents/${name}.md`))
    if (sibling !== null) return sibling.model
    const registry = JSON.parse(await $.fs.read(`${await configDir()}/plugins/installed_plugins.json`)) as {
      plugins?: Record<string, { installPath?: unknown }[]>
    }
    const key = Object.keys(registry.plugins ?? {}).find(k => k.split('@')[0] === plugin)
    const root = key === undefined ? undefined : registry.plugins?.[key]?.[0]?.installPath
    return typeof root === 'string' ? (found(await readDefinition(`${root}/agents/${name}.md`))?.model ?? null) : null
  } catch {
    return null
  }
}

async function run($: EngineInterface, argv: string[], stdin: string) {
  try {
    const done = await $.process.run(argv, { stdin })
    return done.exitCode === 0 ? done.stdout : null
  } catch {
    return null
  }
}

async function gridDefault($: EngineInterface): Promise<Proposal | null> {
  try {
    const grid = JSON.parse(await $.fs.read(`${$.plugin.root}/hooks/subagent-routing/grid.json`)) as {
      default: string
      categories: Record<string, RoutedChoice>
    }
    const found = grid.categories[grid.default]
    return found && isModel(found.model) && isEffort(found.effort) ? { ...found, category: grid.default } : null
  } catch {
    return null
  }
}

// `classify.py` answers the grid's default with an `error` field when the classifier fails; a run that throws, exits
// non-zero or prints nothing readable gets the grid default here.
async function classify($: EngineInterface, prompt: string): Promise<Proposal | null> {
  const root = await $.session.root().catch(() => undefined)
  const argv = ['python3', `${$.plugin.root}/hooks/subagent-routing/classify.py`, ...(root ? ['--repo-root', root] : [])]
  const out = await run($, argv, prompt)
  try {
    const r = JSON.parse(out ?? '') as { category?: unknown; model?: unknown; effort?: unknown; error?: unknown; model_id?: unknown }
    if (typeof r.category === 'string' && isModel(r.model) && isEffort(r.effort)) {
      return { category: r.category, model: r.model, effort: r.effort, ...(typeof r.error === 'string' ? { error: r.error } : {}),
        ...(typeof r.model_id === 'string' ? { classifierModel: r.model_id } : {}),
      }
    }
  } catch {
    // falls through to the grid default
  }
  const fallback = await gridDefault($)
  return fallback && { ...fallback, error: out === null ? 'classifier did not run' : 'classifier answer unreadable' }
}

async function cardOf($: EngineInterface, call: AgentCall): Promise<{ model?: string; effort?: string }> {
  if (call.subagent_type !== CARD_WORKER || typeof call.prompt !== 'string') return {}
  const path = CARD_LINE.exec(call.prompt)?.[1]
  if (path === undefined) return {}
  try {
    return cardChoice(await $.fs.read(path))
  } catch {
    return {}
  }
}

// A card's own values first; the classifier only for what the card leaves out.
async function propose($: EngineInterface, call: AgentCall, prompt: string): Promise<Proposal | null> {
  const card = await cardOf($, call)
  if (card.model !== undefined && card.effort !== undefined) {
    return { model: card.model, effort: card.effort, category: 'card' }
  }
  const ranked = await classify($, prompt)
  if (ranked === null) return null
  return { ...ranked, model: card.model ?? ranked.model, effort: card.effort ?? ranked.effort }
}

async function journal($: EngineInterface, call: AgentCall, prompt: string, got: Proposal, applied: RoutedChoice, exit: Exit) {
  const entry = {
    prompt,
    agent_type: String(call.subagent_type ?? 'general-purpose'),
    requested_model: String(call.model ?? ''),
    requested_effort: '',
    category: got.category,
    applied_model: applied.model,
    applied_effort: applied.effort,
    classifier_model: got.classifierModel ?? '',
    error: got.error ?? '',
    reason: exit,
  }
  await run($, ['python3', `${$.plugin.root}/hooks/subagent-routing/journal.py`], JSON.stringify(entry))
}

// One call is held at a time; each takes its place at arrival, before it is classified, and the others wait their turn. The waits go through `$` calls, whose time
// does not count in the hook's 10 s limit.
const waiting: string[] = []
const accepted = new Set<string>()
// The picks, kept synchronously: the hold loop reads them, the atom only redraws the band.
const picks = new Map<string, RoutedChoice>()

async function pause($: EngineInterface): Promise<boolean> {
  try {
    return (await $.process.run(['sleep', '0.25'])).exitCode === 0
  } catch {
    return false
  }
}

async function hold($: EngineInterface, one: HeldAgent, signal: AbortSignal): Promise<RoutedChoice | 'timeout' | 'aborted'> {
  let showing = false
  try {
    while (waiting[0] !== one.id && !signal.aborted) {
      if (!(await pause($))) return 'timeout'
    }
    if (signal.aborted) return 'aborted'
    showing = true
    await update($, held, () => one)
    const deadline = (await $.clock.now()) + holdLimits.ms
    while (!accepted.has(one.id) && !signal.aborted && (await $.clock.now()) < deadline) {
      if (!(await pause($))) return 'timeout'
    }
    if (accepted.has(one.id)) return picks.get(one.id) ?? one.proposal
    return signal.aborted ? 'aborted' : 'timeout'
  } finally {
    accepted.delete(one.id)
    picks.delete(one.id)
    try {
      if (showing) await update($, held, () => null)
    } catch {
      // Clearing the band is best-effort: the call still leaves with the user's pick.
    }
  }
}

async function route($: EngineInterface, e: any, next: any, call: AgentCall, prompt: string) {
  if (call.model === undefined && call.subagent_type !== CARD_WORKER && typeof call.subagent_type === 'string') {
    if ((await definedModel($, call.subagent_type)) !== null) return next(e)
  }
  const got = await propose($, call, prompt)
  if (got === null) return next(e)
  const go = (choice: RoutedChoice) => next({ ...e, ...choice } as typeof e)

  if (!(await $.session.surfaces()).some(s => s === 'terminal' || s === 'desktop')) {
    await journal($, call, prompt, got, got, 'no-band')
    return go(got)
  }
  const proposal = { model: got.model, effort: got.effort }
  const one: HeldAgent = {
    id: call.tool_use_id,
    agent: clean(String(call.subagent_type ?? 'general-purpose')),
    category: got.category,
    proposal,
    chosen: proposal,
  }
  const outcome = await hold($, one, next.signal)
  if (outcome === 'aborted') return { deny: 'The subagent call was cancelled before the user chose.' }
  const leaves = outcome === 'timeout' ? proposal : outcome
  await journal($, call, prompt, got, leaves, outcome === 'timeout' ? 'timeout' : 'user-choice')
  return go(leaves)
}

export const register: Register = on => {
  on('tool.call', { tool: /^(?:Agent|Task)$/ }, async ($, e, next) => {
    const call = e as unknown as AgentCall
    // A fork inherits the parent's model whatever the call says. A call that names no model to an agent whose definition
    // carries one is the author's choice; the card-worker is proposed its card's values instead.
    if (call.subagent_type === 'fork' || typeof call.prompt !== 'string') return next(e)
    // The place in the queue is taken now, so arrival order holds whichever classification ends first.
    if (waiting.includes(call.tool_use_id)) return next(e)
    waiting.push(call.tool_use_id)
    try {
      return await route($, e, next, call, call.prompt)
    } finally {
      const at = waiting.indexOf(call.tool_use_id)
      if (at !== -1) waiting.splice(at, 1)
    }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const one = await read($, held)
    if (!one || e.props.hasSurvey) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const pick = (change: Partial<RoutedChoice>) => () => {
      if (!waiting.includes(one.id)) return
      const chosen = { ...(picks.get(one.id) ?? one.proposal), ...change }
      picks.set(one.id, chosen)
      void update($, held, cur => (cur && cur.id === one.id ? { ...cur, chosen } : cur))
    }
    const model = (m: string, i: number) => (
      <Button
        key={`model-${m}`}
        label={`${one.chosen.model === m ? '●' : '○'} ${tierMark(m)} ${m}`}
        hotkey={String(i + 1)}
        hover={{ scope: `model-${m}`, color: 'cyan', bold: true }}
        onPress={pick({ model: m })}
      />
    )
    const effort = (f: string, i: number) => (
      <Button
        key={`effort-${f}`}
        label={`${one.chosen.effort === f ? '●' : '○'} ${f}`}
        hotkey={String(i + 4)}
        hover={{ scope: `effort-${f}`, color: 'cyan', bold: true }}
        onPress={pick({ effort: f })}
      />
    )
    return (
      <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
        <Text bold color="cyan">{`Subagent · ${one.agent}`}</Text>
        <Text>{`Proposed ${tierMark(one.proposal.model)} ${one.proposal.model} · ${one.proposal.effort} · ${one.category}`}</Text>
        <Box flexDirection="row" flexWrap="wrap" columnGap={2} marginTop={1}>
          {MODELS.map(model)}
          {EFFORTS.map(effort)}
          <Button
            key="accept"
            label={`✔︎ go with ${tierMark(one.chosen.model)} ${one.chosen.model} · ${one.chosen.effort}`}
            hotkey="0"
            hover={{ scope: 'accept', color: 'green', bold: true }}
            onPress={() => {
              if (waiting.includes(one.id)) accepted.add(one.id)
            }}
          />
        </Box>
      </Box>
    )
  })
}
