import type { Register } from 'claude-code'

// Même chaîne que REASON dans memory-write/memory_write_guard.py : un test Python vérifie l'égalité.
const GUARD_REASON = 'Write into a memory file.'
const DEFAULT_SILO = /\/projects\/[^/]+\/memory\//
const MAX_LENGTH = 200
const BOM = String.fromCharCode(0xfeff)
// Séquences ANSI, puis tout caractère de contrôle : le texte est écrit par le modèle dont l'écriture est jugée.
const CONTROL = /\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x1f\x7f]/g

function clean(text: string): string {
  return text.replace(CONTROL, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_LENGTH)
}

function descriptionOf(content: string): string | undefined {
  const head = /^---\r?\n([\s\S]*?)\r?\n---/.exec(content.replace(new RegExp(`^${BOM}`), ''))
  const line = head ? /^description:[ \t]*(.*)$/m.exec(head[1]) : null
  if (!line) return undefined
  const value = line[1].trim().replace(/^(["'])(.*)\1$/, '$2')
  // `>` et `|` ouvrent un bloc YAML dont le texte est sur les lignes suivantes : pas de description ici.
  return value === '' || /^[>|]/.test(value) ? undefined : clean(value)
}

function levelOf(path: string): string {
  return path.replace(/\/[^/]*$/, '').replace(/^\/(?:Users|home)\/[^/]+/, '~')
}

// Le dialogue de permission montre le `reason` d'un `ask` : on y met le dossier mémoire visé puis la description
// de la note, pour que la personne accepte l'écriture ou la redirige vers un autre niveau.
// Seul `Write` porte le contenu de la note ; `Edit`, `MultiEdit` et `Bash` gardent le motif de la garde.
export const register: Register = on => {
  on('tool.check', async ($, e, next) => {
    const verdict = await next(e)
    try {
      if (verdict.decision !== 'ask') return verdict
      const input = e.input as { file_path?: unknown; content?: unknown } | null
      const path = input?.file_path
      const content = input?.content
      if (typeof path !== 'string' || typeof content !== 'string') return verdict
      if (!DEFAULT_SILO.test(path) && !verdict.reason?.includes(GUARD_REASON)) return verdict
      const description = descriptionOf(content)
      return description ? { ...verdict, reason: `${levelOf(path)}\n${description}` } : verdict
    } catch {
      return verdict
    }
  })
}
