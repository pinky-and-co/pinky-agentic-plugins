// Retient la lecture d'un fichier à secret et laisse la personne choisir l'issue, dans la bande.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { HeldRead } from '../../types'

const MAX_LENGTH = 200
const MAX_LINES_SHOWN = 6
// Séquences ANSI, puis caractères de contrôle, de format (bidi) et séparateurs de ligne : le chemin vient du modèle
// dont la lecture est jugée.
const UNSAFE = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

// Une icône et une couleur par geste ; le bouton lui-même n'a de couleur qu'au survol.
const MARKS = {
  read: { icon: '⚿', color: 'yellow' },
  pragma: { icon: '✎', color: 'green' },
  offline: { icon: '⌂', color: 'cyan' },
  deny: { icon: '✘', color: 'red' },
} as const

// Au-delà de cette durée sans réponse (bande jamais dessinée), la lecture n'est plus retenue : elle retombe sur le
// refus de la garde. Exporté pour que les tests le raccourcissent.
export const holdLimits = { ms: 60_000 }

// Tenu par l'hôte : le dessin se rafraîchit quand une lecture est retenue ou relâchée.
const held = atom({ plugin: 'pc-secrets', key: 'held' } as const, null as HeldRead | null)

export function clean(text: string): string {
  return text.replace(UNSAFE, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_LENGTH)
}

// Sortie de `privacy_egress.py --verdict`, vérifiée champ par champ ; rien d'exploitable : pas de bande.
export function parseVerdict(stdout: string): Omit<HeldRead, 'id'> | null {
  let data: unknown
  try {
    data = JSON.parse(stdout)
  } catch {
    return null
  }
  const v = (data ?? {}) as { flagged?: unknown; path?: unknown; lines?: unknown; digest?: unknown; pragma?: unknown; offline?: unknown }
  if (v.flagged !== true || typeof v.path !== 'string' || typeof v.digest !== 'string' || typeof v.pragma !== 'boolean' || typeof v.offline !== 'boolean') return null
  if (!Array.isArray(v.lines) || v.lines.length === 0 || !v.lines.every(n => Number.isInteger(n) && n > 0)) return null
  return { path: v.path, lines: v.lines as number[], digest: v.digest, pragma: v.pragma, offline: v.offline }
}

async function verdictOf($: EngineInterface, path: string) {
  try {
    const stdin = JSON.stringify({ tool_name: 'Read', tool_input: { file_path: path } })
    const done = await $.process.run(['python3', `${$.plugin.root}/hooks/privacy_egress.py`, '--verdict'], { stdin })
    return done.exitCode === 0 ? parseVerdict(done.stdout) : null
  } catch {
    return null
  }
}

// Écrit le marqueur sur les lignes signalées ; le nombre de lignes marquées, 0 si le script échoue.
async function markLines($: EngineInterface, path: string, digest: string, lines: number[]): Promise<number> {
  try {
    const done = await $.process.run(['python3', `${$.plugin.root}/hooks/privacy_egress.py`, '--pragma', path, digest, ...lines.map(String)])
    return done.exitCode === 0 ? Number(done.stdout.trim()) || 0 : 0
  } catch {
    return 0
  }
}

type Choice = 'pragma' | 'offline' | 'deny'
const decisions = new Map<string, Choice>()

function decide(id: string, choice: Choice) {
  // La première pression gagne : un second appui ne rend pas plus permissive une réponse déjà donnée ; une pression
  // tardive ne laisse rien derrière elle.
  if (waiting.includes(id) && !decisions.has(id)) decisions.set(id, choice)
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
  if (waiting.includes(one.id)) return 'expired' // le même appel deux fois : le second tombe sur le refus de la garde
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
    // le refus de la garde s'applique tout de suite, sans attente.
    if (!(await $.session.surfaces()).some(s => s === 'terminal' || s === 'desktop')) return next(e)

    // Un refus du moteur ou une règle des réglages décide déjà : ni bande, ni écriture dans le fichier.
    const engine = await $.tool.check({ tool: 'Read', input: { file_path: e.file_path } })
    if (engine.decision === 'deny' || engine.rule !== undefined) return next(e)

    // La garde Python reste la source du verdict : sans fichier signalé, ou sans réponse, rien n'est retenu.
    const verdict = await verdictOf($, e.file_path)
    if (verdict === null) return next(e)
    const one: HeldRead = { ...verdict, id: e.tool_use_id }

    const choice = await hold($, one, next.signal)
    if (choice === 'expired') return next(e) // personne n'a répondu : la garde refuse
    if (choice === 'deny') {
      return { deny: `The user refused to let you read ${clean(verdict.path)}: it holds secret-shaped data. Ask what to do instead.` }
    }
    if (choice === 'offline') {
      return {
        deny:
          `The user chose not to send ${clean(verdict.path)} to the cloud model, which holds secret-shaped data. ` +
          `Do not read it. Run the task locally with the \`offline\` tool of pc-core (files: ["${verdict.path}"]).`,
      }
    }

    const marked = await markLines($, verdict.path, verdict.digest, verdict.lines)
    const result = await next(e) // la garde rejuge le fichier : les lignes marquées ne comptent plus
    if (marked === 0 || result.deny !== undefined || result.isError) return result
    const note = `The user marked ${marked} line(s) of ${clean(verdict.path)} as not secret (\`# pragma: allowlist secret\` added at the end of each) and allowed this read.`
    return { ...result, context: [...(result.context ?? []), note] }
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
    const shown = one.lines.slice(0, MAX_LINES_SHOWN).join(', ') + (one.lines.length > MAX_LINES_SHOWN ? ', …' : '')
    return (
      <Box flexDirection="column" borderStyle="round" borderColor={MARKS.read.color} paddingX={1}>
        <Box flexDirection="row" justifyContent="space-between" gap={2}>
          <Text bold color={MARKS.read.color}>{`${MARKS.read.icon} Secret-shaped data`}</Text>
          <Text dimColor wrap="truncate-start">{clean(one.path)}</Text>
        </Box>
        <Text wrap="truncate-end">{`line ${shown}`}</Text>
        <Box flexDirection="row" flexWrap="wrap" columnGap={2} marginTop={1}>
          {one.pragma && choice('pragma', MARKS.pragma, 'add pragma', '3', 'pragma')}
          {one.offline && choice('offline', MARKS.offline, 'delegate offline', '4', 'offline')}
          {choice('deny', MARKS.deny, 'deny', '9', 'deny')}
        </Box>
      </Box>
    )
  })
}
