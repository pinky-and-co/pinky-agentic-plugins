// Retient l'écriture d'une note dans un dossier mémoire et laisse la personne choisir son niveau, dans la bande.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { HeldWrite, MemoryLevel } from '../../types'

const MAX_LENGTH = 200
const MAX_REMEMBERED = 50
const MAX_LEVELS = 8 // touches 1 à 8 ; 0 refuse
const BOM = String.fromCharCode(0xfeff)
// Le dossier mémoire par défaut : `<config>/projects/<projet>/memory/`. Un autre emplacement (autoMemoryDirectory,
// CLAUDE_CONFIG_DIR) n'est pas retenu et garde le seul dialogue natif, que la garde Python déclenche toujours.
const MEMORY_DIR = /^\/(?:.+\/)?\.claude\/projects\/[^/]+\/memory\//
const ROOT_INDEX = /^\/(?:.+\/)?\.claude\/projects\/[^/]+\/memory\/MEMORY\.md$/
// Un segment `.`, `..` ou vide : le chemin n'est pas celui que la garde Python normalise et juge.
const DOT_SEGMENT = /\/\.{1,2}(?:\/|$)|\/\//
// Séquences ANSI, puis caractères de contrôle, de format (bidi) et séparateurs de ligne : le texte est écrit par
// le modèle dont l'écriture est jugée.
const UNSAFE = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

// Une icône et une couleur par geste ; le bouton lui-même n'a de couleur qu'au survol.
const MARKS = {
  write: { icon: '✍︎', color: 'cyan' },
  here: { icon: '✔︎', color: 'green' },
  moveTo: { icon: '⎘', color: 'yellow' },
  refuse: { icon: '✘', color: 'red' },
} as const

// Au-delà de cette durée sans réponse (bande jamais dessinée), l'écriture n'est plus retenue : elle retombe sur le
// `ask` de la garde et le dialogue natif. Exporté pour que les tests le raccourcissent.
export const holdLimits = { ms: 60_000 }

// Tenu par l'hôte : le dessin se rafraîchit quand une écriture est retenue ou relâchée.
const held = atom({ plugin: 'pc-memory', key: 'held' } as const, null as HeldWrite | null)

// L'index MEMORY.md à la racine d'un dossier mémoire est exempté, comme dans memory_write_guard.py.
export function isMemoryNote(path: string): boolean {
  return MEMORY_DIR.test(path) && !ROOT_INDEX.test(path) && !DOT_SEGMENT.test(path)
}

export function clean(text: string): string {
  return text.replace(UNSAFE, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_LENGTH)
}

// Lie l'accord de la personne à ce qu'elle a vu : un `tool.call` plus bas qui réécrirait l'appel n'est pas levé.
function fingerprint(path: string, content: string): string {
  let h = 2166136261
  for (let i = 0; i < content.length; i++) {
    h ^= content.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return `${path}\n${content.length}\n${h >>> 0}`
}

export function descriptionOf(content: string): string {
  const head = /^---\r?\n([\s\S]*?)\r?\n---/.exec(content.replace(new RegExp(`^${BOM}`), ''))
  const line = head?.[1] === undefined ? null : /^description:[ \t]*(.*)$/m.exec(head[1])
  const raw = (line?.[1] ?? '').trim().replace(/^(["'])(.*)\1$/, '$2')
  // `>` et `|` ouvrent un bloc YAML dont le texte est sur les lignes suivantes : pas de description ici.
  const value = /^[>|]/.test(raw) ? '' : clean(raw)
  return value === '' ? '(no description)' : value
}

// Sortie de `levels.py`, vérifiée champ par champ ; le niveau visé d'abord. Rien d'exploitable : aucun niveau.
export function parseLevels(stdout: string): { levels: MemoryLevel[]; relative: string | null } {
  const none = { levels: [], relative: null }
  let data: unknown
  try {
    data = JSON.parse(stdout)
  } catch {
    return none
  }
  const { levels, relative } = (data ?? {}) as { levels?: unknown; relative?: unknown }
  if (!Array.isArray(levels) || (relative !== null && typeof relative !== 'string')) return none
  const valid = levels.filter(
    (l): l is MemoryLevel =>
      typeof l?.name === 'string' && typeof l?.dir === 'string' && typeof l?.current === 'boolean' && typeof l?.target === 'boolean',
  )
  const target = valid.filter(l => l.target)
  if (target.length !== 1 || relative === null || DOT_SEGMENT.test(`/${relative}`)) return none
  const others = valid.filter(l => !l.target).slice(0, MAX_LEVELS - 1)
  return { levels: [...target, ...others].map(l => ({ ...l, name: clean(l.name) })), relative }
}

async function levelsOf($: EngineInterface, path: string) {
  try {
    // La racine du projet, pas le dossier courant du shell : Claude Code y rattache sa mémoire, un `cd` ne la déplace pas.
    const root = await $.session.root()
    const done = await $.process.run(['python3', `${$.plugin.root}/hooks/memory-hold/levels.py`, root, path])
    return done.exitCode === 0 ? parseLevels(done.stdout) : parseLevels('')
  } catch {
    return parseLevels('')
  }
}

// Ajoute la note déplacée à l'index de son nouveau niveau ; `false` si le script échoue (le modèle indexera).
async function indexAt($: EngineInterface, dir: string, relative: string): Promise<boolean> {
  try {
    const done = await $.process.run(['python3', `${$.plugin.root}/hooks/memory-hold/index_note.py`, dir, relative])
    return done.exitCode === 0
  } catch {
    return false
  }
}

type Choice = { level: number } | 'refuse'
const decisions = new Map<string, Choice>()
const accepted = new Map<string, string>()

function decide(id: string, choice: Choice) {
  if (waiting.includes(id)) decisions.set(id, choice) // une pression tardive ne laisse rien derrière elle
}

// Une écriture est retenue à la fois ; les suivantes attendent leur tour dans l'ordre d'arrivée. L'attente passe par des
// appels `$`, dont le temps ne compte pas dans la limite de 10 s du hook (une promesse nue la ferait compter).
const waiting: string[] = []

// Une pause d'un quart de seconde ; `false` si elle échoue (pas de `sleep`) : on cesse d'attendre au lieu de boucler.
async function pause($: EngineInterface): Promise<boolean> {
  try {
    const done = await $.process.run(['sleep', '0.25'])
    return done.exitCode === 0
  } catch {
    return false
  }
}

function remember(id: string, print: string) {
  accepted.set(id, print)
  if (accepted.size > MAX_REMEMBERED) accepted.delete(accepted.keys().next().value as string)
}

async function hold($: EngineInterface, one: HeldWrite, signal: AbortSignal): Promise<Choice | 'expired'> {
  if (waiting.includes(one.id)) return 'expired' // le même appel deux fois : le second tombe sur le dialogue natif
  let showing = false
  waiting.push(one.id)
  try {
    // Pas de délai propre pour attendre son tour : la tête de file est toujours bornée (sa propre minute, l'abandon ou
    // une pause qui échoue). Une fois son tour venu, chaque écriture a sa minute.
    while (waiting[0] !== one.id && !signal.aborted) {
      if (!(await pause($))) return 'expired'
    }
    if (signal.aborted) return 'refuse'

    showing = true
    await update($, held, () => one)
    const deadline = (await $.clock.now()) + holdLimits.ms
    // Le temps passé dans les appels `$` ne compte pas dans la limite de temps du hook.
    while (!decisions.has(one.id) && !signal.aborted && (await $.clock.now()) < deadline) {
      if (!(await pause($))) return 'expired'
    }
    const choice = decisions.get(one.id)
    if (choice !== undefined) return choice
    return signal.aborted ? 'refuse' : 'expired'
  } finally {
    // La place dans la file n'est rendue qu'après l'effacement de la bande : le suivant ne peut pas courir avec elle.
    decisions.delete(one.id)
    try {
      if (showing) await update($, held, () => null)
    } finally {
      const at = waiting.indexOf(one.id)
      if (at !== -1) waiting.splice(at, 1)
    }
  }
}

export const register: Register = on => {
  on('tool.call', { tool: 'Write' }, async ($, e, next) => {
    if (typeof e.file_path !== 'string' || typeof e.content !== 'string' || !isMemoryNote(e.file_path)) return next(e)
    // La même porte de sortie que la garde Python : la personne a demandé de ne pas être interrogée.
    if ((await $.env.get('PC_ALLOW_MEMORY_WRITE')) === '1') return next(e)
    // Aucune interface qui dessine la bande (`claude -p`) : le `ask` de la garde et le dialogue natif s'appliquent.
    if ((await $.session.surfaces()).length === 0) return next(e)

    // `$.tool.check` ne voit pas le `ask` de la garde Python (aucun hook PreToolUse n'y est consulté) : en mode `auto` ou
    // avec `~/.claude` en répertoire de travail il répond `allow`, et la garde pose quand même son `ask`. L'écriture est
    // donc retenue sauf refus du moteur ou verdict tiré d'une règle des réglages, que l'accord ne lèverait pas.
    const verdict = await $.tool.check({ tool: 'Write', input: { file_path: e.file_path, content: e.content } })
    if (verdict.decision === 'deny' || verdict.rule !== undefined) return next(e)

    const text = e.content.replace(new RegExp(`^${BOM}`), '')
    const { levels, relative } = await levelsOf($, e.file_path)
    const one: HeldWrite = {
      id: e.tool_use_id,
      description: descriptionOf(e.content),
      file: clean(e.file_path.split('/').pop() ?? ''),
      lines: text === '' ? 0 : text.replace(/\r?\n$/, '').split('\n').length,
      levels,
      relative,
    }
    const choice = await hold($, one, next.signal)
    if (choice === 'expired') return next(e) // personne n'a répondu : le `ask` de la garde et le dialogue natif s'appliquent
    if (choice === 'refuse') {
      return { deny: `The user refused this memory note (${clean(e.file_path)}). Ask what to change or where it belongs.` }
    }

    const level = levels[choice.level]
    const moved = level !== undefined && !level.target && relative !== null ? `${level.dir}/${relative}` : null
    if (moved !== null && !isMemoryNote(moved)) return next(e) // un niveau hors du dossier par défaut : dialogue natif
    const path = moved ?? e.file_path
    remember(e.tool_use_id, fingerprint(path, e.content))
    if (moved === null || level === undefined) return next(e)
    const result = await next({ ...e, file_path: moved })
    if (result.deny !== undefined || result.isError) return result
    const where = `The user moved this memory note to the ${level.name} level: it was written at ${moved}, not at ${e.file_path}.`
    const indexed = await indexAt($, level.dir, relative as string)
    const note = indexed
      ? `${where} It is already listed in that level's MEMORY.md. If you added a line for it to the MEMORY.md of the level first aimed at, remove that line.`
      : `${where} Update that level's MEMORY.md index, not the one of the level first aimed at.`
    return { ...result, context: [...(result.context ?? []), note] }
  })

  on('tool.check', { tool: 'Write' }, async ($, e, next) => {
    const verdict = await next(e)
    // Un `ask` posé par une règle des réglages (`rule`) reste un `ask`.
    if (verdict.decision !== 'ask' || verdict.rule !== undefined || e.tool_use_id === undefined) return verdict
    const input = e.input as { file_path?: unknown; content?: unknown } | null
    if (typeof input?.file_path !== 'string' || typeof input?.content !== 'string') return verdict
    if (accepted.get(e.tool_use_id) !== fingerprint(input.file_path, input.content)) return verdict
    return { decision: 'allow' as const, reason: 'Accepted in the memory band.' }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const one = await read($, held)
    if (!one || e.props.hasSurvey) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const [target, ...others] = one.levels
    const here = target === undefined ? 'write' : target.current ? 'here' : target.name
    // Les chiffres restent des raccourcis depuis un prompt vide ; ils ne sont pas dessinés.
    const choice = (key: string, mark: (typeof MARKS)[keyof typeof MARKS], label: string, hotkey: string, pick: Choice) => (
      <Button
        key={key}
        label={`${mark.icon} ${label}`}
        hotkey={hotkey}
        hover={{ scope: key, color: mark.color, bold: true }}
        onPress={() => decide(one.id, pick)}
      />
    )
    return (
      <Box flexDirection="column" borderStyle="round" borderColor={MARKS.write.color} paddingX={1}>
        <Box flexDirection="row" justifyContent="space-between" gap={2}>
          <Text bold color={MARKS.write.color}>{`${MARKS.write.icon} Memory note`}</Text>
          <Text dimColor wrap="truncate-start">{`${one.file} · ${one.lines} l.`}</Text>
        </Box>
        <Text wrap="truncate-end">{one.description}</Text>
        <Box flexDirection="row" flexWrap="wrap" columnGap={2} marginTop={1}>
          {choice('here', MARKS.here, here, '1', { level: 0 })}
          {others.map((level, i) => choice(`level-${i + 1}`, MARKS.moveTo, level.name, String(i + 2), { level: i + 1 }))}
          {choice('refuse', MARKS.refuse, 'refuse', '0', 'refuse')}
        </Box>
      </Box>
    )
  })
}
