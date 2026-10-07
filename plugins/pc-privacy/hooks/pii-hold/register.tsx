// Retient la lecture d'un jeu de données personnelles et laisse la personne choisir l'issue, dans la bande.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { HeldRead } from '../../types'

const MAX_LENGTH = 200
const MAX_REMEMBERED = 50
const MAX_COLUMNS_SHOWN = 5
// L'analyse (Presidio + spaCy) se charge à froid dans `uv run` : bien plus que les 30 s par défaut d'un appel `$`.
const SCAN_TIMEOUT_MS = 55_000
// Séquences ANSI, puis caractères de contrôle, de format (bidi) et séparateurs de ligne : les noms de colonnes viennent
// du fichier dont la lecture est jugée.
const UNSAFE = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

// Une icône et une couleur par geste ; le bouton lui-même n'a de couleur qu'au survol.
const MARKS = {
  read: { icon: '⚠︎', color: 'yellow' },
  anonymize: { icon: '⎘', color: 'cyan' },
  allow: { icon: '✔︎', color: 'green' },
  deny: { icon: '✘', color: 'red' },
} as const

// Au-delà de cette durée sans réponse (bande jamais dessinée), la lecture n'est plus retenue : elle retombe sur le
// `ask` de la garde et le dialogue natif. Exporté pour que les tests le raccourcissent.
export const holdLimits = { ms: 60_000 }

// Tenu par l'hôte : le dessin se rafraîchit quand une lecture est retenue ou relâchée.
const held = atom({ plugin: 'pc-privacy', key: 'held' } as const, null as HeldRead | null)

export function clean(text: string): string {
  return text.replace(UNSAFE, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_LENGTH)
}

// Sortie de `pii_guard.py --verdict`, vérifiée champ par champ ; rien d'exploitable : pas de bande.
export function parseVerdict(stdout: string): Omit<HeldRead, 'id' | 'file'> | null {
  let data: unknown
  try {
    data = JSON.parse(stdout)
  } catch {
    return null
  }
  const v = (data ?? {}) as { flagged?: unknown; kind?: unknown; path?: unknown; columns?: unknown; anonymize?: unknown }
  if (v.flagged !== true || (v.kind !== 'pii' && v.kind !== 'oversize') || typeof v.path !== 'string') return null
  if (typeof v.anonymize !== 'boolean' || !Array.isArray(v.columns) || !v.columns.every(c => typeof c === 'string')) return null
  return { path: v.path, kind: v.kind, columns: v.columns.map(clean), anonymize: v.anonymize }
}

async function verdictOf($: EngineInterface, path: string) {
  try {
    const stdin = JSON.stringify({ tool_name: 'Read', tool_input: { file_path: path } })
    const done = await $.process.run(['uv', 'run', '--script', `${$.plugin.root}/hooks/pii_guard.py`, '--verdict'], { stdin, timeoutMs: SCAN_TIMEOUT_MS })
    return done.exitCode === 0 ? parseVerdict(done.stdout) : null
  } catch {
    return null
  }
}

// Le chemin de la copie anonymisée ; `null` si le script échoue ou n'a rien masqué.
async function copyOf($: EngineInterface, path: string): Promise<string | null> {
  try {
    const done = await $.process.run(['uv', 'run', '--script', `${$.plugin.root}/hooks/pii_guard.py`, '--anonymize', path], { timeoutMs: SCAN_TIMEOUT_MS })
    if (done.exitCode !== 0) return null
    const copy = (JSON.parse(done.stdout) as { copy?: unknown }).copy
    return typeof copy === 'string' && copy !== '' ? copy : null
  } catch {
    return null
  }
}

type Choice = 'anonymize' | 'allow' | 'deny'
const decisions = new Map<string, Choice>()
const accepted = new Map<string, string>()

function decide(id: string, choice: Choice) {
  // La première pression gagne : un second appui ne rend pas plus permissive une réponse déjà donnée ; une pression
  // tardive ne laisse rien derrière elle.
  if (waiting.includes(id) && !decisions.has(id)) decisions.set(id, choice)
}

function remember(id: string, path: string) {
  accepted.set(id, path)
  if (accepted.size > MAX_REMEMBERED) accepted.delete(accepted.keys().next().value as string)
}

// Une lecture est retenue à la fois ; les suivantes attendent leur tour dans l'ordre d'arrivée. L'attente passe par des
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

async function hold($: EngineInterface, one: HeldRead, signal: AbortSignal): Promise<Choice | 'expired'> {
  if (waiting.includes(one.id)) return 'expired' // le même appel deux fois : le second tombe sur le dialogue natif
  let showing = false
  waiting.push(one.id)
  try {
    // La tête de file est toujours bornée (sa propre minute, l'abandon ou une pause qui échoue) : pas de délai propre.
    while (waiting[0] !== one.id && !signal.aborted) {
      if (!(await pause($))) return 'expired'
    }
    if (signal.aborted) return 'deny'

    showing = true
    await update($, held, () => one)
    const deadline = (await $.clock.now()) + holdLimits.ms
    while (!decisions.has(one.id) && !signal.aborted && (await $.clock.now()) < deadline) {
      if (!(await pause($))) return 'expired'
    }
    const choice = decisions.get(one.id)
    if (choice !== undefined) return choice
    return signal.aborted ? 'deny' : 'expired'
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
  on('tool.call', { tool: 'Read' }, async ($, e, next) => {
    if (typeof e.file_path !== 'string') return next(e)
    // La bande (`AbovePrompt`) ne se dessine que dans le terminal et le desktop : ailleurs (`claude -p`, VS Code seul),
    // le `ask` de la garde et le dialogue natif s'appliquent tout de suite, sans attente.
    if (!(await $.session.surfaces()).some(s => s === 'terminal' || s === 'desktop')) return next(e)

    // `$.tool.check` ne voit pas le `ask` de la garde Python : la lecture est retenue sauf refus du moteur ou verdict tiré
    // d'une règle des réglages, que l'accord ne lèverait pas.
    const engine = await $.tool.check({ tool: 'Read', input: { file_path: e.file_path } })
    if (engine.decision === 'deny' || engine.rule !== undefined) return next(e)

    // La garde Python reste la source du verdict : sans fichier signalé, ou sans réponse, rien n'est retenu.
    const verdict = await verdictOf($, e.file_path)
    if (verdict === null) return next(e)
    const one: HeldRead = { ...verdict, id: e.tool_use_id, file: clean(verdict.path.split('/').pop() ?? '') }

    const choice = await hold($, one, next.signal)
    if (choice === 'expired') return next(e) // personne n'a répondu : le `ask` de la garde et le dialogue natif s'appliquent
    if (choice === 'deny') {
      return {
        deny: `The user refused to let you read ${clean(verdict.path)}: it holds personal data. Ask what to do instead; an anonymized copy can be made with \`anonymize_apply\` from \`pc-privacy\`.`,
      }
    }
    if (choice === 'allow') {
      remember(e.tool_use_id, e.file_path) // le chemin tel que le moteur le verra, pas sa forme normalisée
      return next(e)
    }

    const copy = await copyOf($, verdict.path)
    if (copy === null) {
      return { deny: `The user asked for an anonymized copy of ${clean(verdict.path)} but none could be made. Tell them and ask what to do instead.` }
    }
    remember(e.tool_use_id, copy)
    const result = await next({ ...e, file_path: copy })
    if (result.deny !== undefined || result.isError) return result
    const note = `The user chose an anonymized copy: you read ${copy}, not ${verdict.path}. Its personal columns are replaced by pseudonyms; say so when you quote it.`
    return { ...result, context: [...(result.context ?? []), note] }
  })

  on('tool.check', { tool: 'Read' }, async ($, e, next) => {
    const verdict = await next(e)
    // Un `ask` posé par une règle des réglages (`rule`) reste un `ask`.
    if (verdict.decision !== 'ask' || verdict.rule !== undefined || e.tool_use_id === undefined) return verdict
    const input = e.input as { file_path?: unknown } | null
    if (typeof input?.file_path !== 'string' || accepted.get(e.tool_use_id) !== input.file_path) return verdict
    return { decision: 'allow' as const, reason: 'Accepted in the personal-data band.' }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const one = await read($, held)
    if (!one || e.props.hasSurvey) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
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
    const columns = one.columns.slice(0, MAX_COLUMNS_SHOWN).join(', ') + (one.columns.length > MAX_COLUMNS_SHOWN ? ', …' : '')
    const detail = one.kind === 'oversize' ? 'too large to analyze, may hold personal data' : columns === '' ? 'judged to hold personal data' : columns
    return (
      <Box flexDirection="column" borderStyle="round" borderColor={MARKS.read.color} paddingX={1}>
        <Box flexDirection="row" justifyContent="space-between" gap={2}>
          <Text bold color={MARKS.read.color}>{`${MARKS.read.icon} Personal data`}</Text>
          <Text dimColor wrap="truncate-start">{one.file}</Text>
        </Box>
        <Text wrap="truncate-end">{detail}</Text>
        <Box flexDirection="row" flexWrap="wrap" columnGap={2} marginTop={1}>
          {one.anonymize && choice('anonymize', MARKS.anonymize, 'anonymize', '1', 'anonymize')}
          {choice('allow', MARKS.allow, 'allow read', '2', 'allow')}
          {choice('deny', MARKS.deny, 'deny', '0', 'deny')}
        </Box>
      </Box>
    )
  })
}
