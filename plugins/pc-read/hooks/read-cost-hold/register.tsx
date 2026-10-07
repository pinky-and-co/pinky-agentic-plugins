// Retient la lecture d'un fichier coûteux à lire tel quel et laisse la personne choisir l'issue, dans la bande.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { HeldRead } from '../../types'

const MAX_LENGTH = 200
const MAX_REMEMBERED = 50
// L'analyse (Presidio + spaCy) de `pc-privacy` se charge à froid dans `uv run` : bien plus que les 30 s par défaut d'un appel `$`.
const SCAN_TIMEOUT_MS = 55_000
// Une conversion de PDF lourd dépasse les 30 s par défaut d'un appel `$`.
const CONVERT_TIMEOUT_MS = 300_000
const INSTALL = "uv tool install 'markitdown[all]'"
// Séquences ANSI, puis caractères de contrôle, de format (bidi) et séparateurs de ligne : le chemin vient du modèle
// dont la lecture est jugée.
const UNSAFE = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

// Une icône et une couleur par geste ; le bouton lui-même n'a de couleur qu'au survol.
const MARKS = {
  read: { icon: '⚠︎', color: 'yellow' },
  convert: { icon: '⎘', color: 'cyan' },
  allow: { icon: '✔︎', color: 'green' },
  deny: { icon: '✘', color: 'red' },
} as const

// Au-delà de cette durée sans réponse (bande jamais dessinée), la lecture n'est plus retenue : elle retombe sur le
// `ask` du hook et le dialogue natif. Exporté pour que les tests le raccourcissent.
export const holdLimits = { ms: 60_000 }

// Publié par `pc-privacy` au démarrage de la session ; non écrit, ce plugin est absent.
const privacyRoot = { plugin: 'pc-privacy', key: 'root' } as const

// Tenu par l'hôte : le dessin se rafraîchit quand une lecture est retenue ou relâchée.
const held = atom({ plugin: 'pc-read', key: 'held' } as const, null as HeldRead | null)

export function clean(text: string): string {
  return text.replace(UNSAFE, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_LENGTH)
}

// Sortie de `read_cost.py --verdict`, vérifiée champ par champ ; rien d'exploitable : pas de bande.
export function parseVerdict(stdout: string): Omit<HeldRead, 'id' | 'file'> | null {
  let data: unknown
  try {
    data = JSON.parse(stdout)
  } catch {
    return null
  }
  const v = (data ?? {}) as { flagged?: unknown; path?: unknown; markitdown?: unknown }
  if (v.flagged !== true || typeof v.path !== 'string' || typeof v.markitdown !== 'boolean') return null
  return { path: v.path, convert: v.markitdown }
}

async function verdictOf($: EngineInterface, path: string) {
  try {
    const stdin = JSON.stringify({ tool_name: 'Read', tool_input: { file_path: path } })
    const done = await $.process.run(['python3', `${$.plugin.root}/hooks/read_cost.py`, '--verdict'], { stdin })
    return done.exitCode === 0 ? parseVerdict(done.stdout) : null
  } catch {
    return null
  }
}

// Faut-il laisser la bande de `pc-privacy` décider ? Oui si sa garde signale ce fichier, ou si elle est publiée mais ne
// répond pas bien (échec, délai, sortie illisible) : retenir ici une lecture qu'elle aurait pu signaler lèverait son `ask`
// par « tel quel ». Seul un `flagged: false` rendu avec succès, ou l'absence de `pc-privacy` (dossier non publié ou
// illisible), laisse la retenue à ce plugin.
async function privacyDecides($: EngineInterface, path: string): Promise<boolean> {
  let root: unknown
  try {
    root = (await $.state.get(privacyRoot)).value
  } catch {
    return false
  }
  if (typeof root !== 'string' || root === '') return false
  try {
    const stdin = JSON.stringify({ tool_name: 'Read', tool_input: { file_path: path } })
    const done = await $.process.run(['uv', 'run', '--script', `${root}/hooks/pii_guard.py`, '--verdict'], { stdin, timeoutMs: SCAN_TIMEOUT_MS })
    return !(done.exitCode === 0 && (JSON.parse(done.stdout) as { flagged?: unknown }).flagged === false)
  } catch {
    return true
  }
}

// Le chemin du Markdown produit ; `null` si le dossier cible ne se crée pas ou si `markitdown` échoue (le dossier est
// alors retiré : seul un Markdown produit reste sur disque).
async function convertTo($: EngineInterface, path: string): Promise<string | null> {
  let markdown: string | null = null
  try {
    const made = await $.process.run(['python3', `${$.plugin.root}/hooks/read_cost.py`, '--target', path])
    if (made.exitCode !== 0) return null
    const target = (JSON.parse(made.stdout) as { markdown?: unknown }).markdown
    if (typeof target !== 'string' || target === '') return null
    markdown = target
    // `--` : un chemin qui commence par `-` n'est pas une option.
    const done = await $.process.run(['markitdown', '-o', markdown, '--', path], { timeoutMs: CONVERT_TIMEOUT_MS })
    if (done.exitCode === 0) return markdown
  } catch {
    // échec ou délai dépassé : même sortie que l'échec ci-dessous
  }
  if (markdown !== null) {
    try {
      await $.process.run(['python3', `${$.plugin.root}/hooks/read_cost.py`, '--discard', markdown])
    } catch {
      // le dossier reste dans le répertoire temporaire du système
    }
  }
  return null
}

type Choice = 'convert' | 'allow' | 'deny'
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
    // le `ask` du hook et le dialogue natif s'appliquent tout de suite, sans attente.
    if (!(await $.session.surfaces()).some(s => s === 'terminal' || s === 'desktop')) return next(e)

    // `$.tool.check` ne voit pas le `ask` du hook Python : la lecture est retenue sauf refus du moteur ou verdict tiré
    // d'une règle des réglages, que l'accord ne lèverait pas.
    const engine = await $.tool.check({ tool: 'Read', input: { file_path: e.file_path } })
    if (engine.decision === 'deny' || engine.rule !== undefined) return next(e)

    // Le hook Python reste la source du verdict : sans fichier signalé, ou sans réponse, rien n'est retenu.
    const verdict = await verdictOf($, e.file_path)
    if (verdict === null || (await privacyDecides($, e.file_path))) return next(e)
    const one: HeldRead = { ...verdict, id: e.tool_use_id, file: clean(verdict.path.split('/').pop() ?? '') }

    const choice = await hold($, one, next.signal)
    if (choice === 'expired') return next(e) // personne n'a répondu : le `ask` du hook et le dialogue natif s'appliquent
    if (choice === 'deny') {
      return { deny: `The user refused to let you read ${clean(verdict.path)}. Ask what to do instead.` }
    }
    if (choice === 'allow') {
      remember(e.tool_use_id, e.file_path) // le chemin tel que le moteur le verra, pas sa forme normalisée
      return next(e)
    }

    const markdown = await convertTo($, verdict.path)
    if (markdown === null) {
      return { deny: `The user asked to convert ${clean(verdict.path)} to Markdown but the conversion failed. Tell them and ask what to do instead.` }
    }
    return { deny: `The user chose to read ${clean(verdict.path)} as Markdown: read ${markdown} instead (converted with markitdown).` }
  })

  on('tool.check', { tool: 'Read' }, async ($, e, next) => {
    const verdict = await next(e)
    // Un `ask` posé par une règle des réglages (`rule`) reste un `ask`.
    if (verdict.decision !== 'ask' || verdict.rule !== undefined || e.tool_use_id === undefined) return verdict
    const input = e.input as { file_path?: unknown } | null
    if (typeof input?.file_path !== 'string' || accepted.get(e.tool_use_id) !== input.file_path) return verdict
    return { decision: 'allow' as const, reason: 'Accepted in the costly-read band.' }
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
    return (
      <Box flexDirection="column" borderStyle="round" borderColor={MARKS.read.color} paddingX={1}>
        <Box flexDirection="row" justifyContent="space-between" gap={2}>
          <Text bold color={MARKS.read.color}>{`${MARKS.read.icon} Costly to read as-is`}</Text>
          <Text dimColor wrap="truncate-start">{one.file}</Text>
        </Box>
        <Text wrap="truncate-end">{one.convert ? 'a native read loads the whole file into context' : `markitdown is not installed: ${INSTALL}`}</Text>
        <Box flexDirection="row" flexWrap="wrap" columnGap={2} marginTop={1}>
          {one.convert && choice('convert', MARKS.convert, 'convert to Markdown', '1', 'convert')}
          {choice('allow', MARKS.allow, 'read as-is', '2', 'allow')}
          {choice('deny', MARKS.deny, 'deny', '0', 'deny')}
        </Box>
      </Box>
    )
  })
}
