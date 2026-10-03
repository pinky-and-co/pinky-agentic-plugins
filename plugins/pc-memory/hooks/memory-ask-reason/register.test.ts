import { test, expect } from 'claude-code/testing'

const GUARD = { decision: 'ask' as const, reason: 'Write into a memory file.' }
const PATH = '/Users/p/.claude/projects/-Users-p-Workspaces-pinky/memory/x.md'
const LEVEL = '~/.claude/projects/-Users-p-Workspaces-pinky/memory'
const note = (description: string, eol = '\n') =>
  ['---', 'name: x', description, 'metadata:', '  type: user', '---', 'corps'].join(eol)

test('shows the memory directory then the note description', async ($, on) => {
  on('tool.check', () => GUARD)
  const out = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content: note('description: Une note sur le niveau workspace') } })
  expect(out.decision).toBe('ask')
  expect(out.reason).toBe(`${LEVEL}\nUne note sur le niveau workspace`)
})

test('strips the quotes of a quoted description', async ($, on) => {
  on('tool.check', () => GUARD)
  const out = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content: note('description: "Une note: avec deux points"') } })
  expect(out.reason).toBe(`${LEVEL}\nUne note: avec deux points`)
})

test('reads a description in CRLF frontmatter behind a BOM', async ($, on) => {
  on('tool.check', () => GUARD)
  const content = String.fromCharCode(0xfeff) + note('description: Fin de ligne Windows', '\r\n')
  const out = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content } })
  expect(out.reason).toBe(`${LEVEL}\nFin de ligne Windows`)
})

test('keeps the guard reason for an empty or block-scalar description', async ($, on) => {
  on('tool.check', () => GUARD)
  for (const description of ['description:', 'description: >-', 'description: |']) {
    const out = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content: note(description) } })
    expect(out.reason).toBe(GUARD.reason)
  }
})

test('cuts a long multi-line description and drops control characters', async ($, on) => {
  on('tool.check', () => GUARD)
  const long = 'a'.repeat(300)
  const out = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content: note(`description: \u001b[31m${long}`) } })
  expect(out.reason).toBe(`${LEVEL}\n${'a'.repeat(200)}`)
})

test('keeps the guard reason without a description or without frontmatter', async ($, on) => {
  on('tool.check', () => GUARD)
  const out = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content: 'sans frontmatter' } })
  expect(out.reason).toBe(GUARD.reason)
})

test('leaves Edit and Bash untouched, they carry no note content', async ($, on) => {
  on('tool.check', () => GUARD)
  const edit = await $.tool.check({ tool: 'Edit', input: { file_path: PATH, old_string: 'a', new_string: 'b' } })
  expect(edit.reason).toBe(GUARD.reason)
  const bash = await $.tool.check({ tool: 'Bash', input: { command: 'echo hi > x.md' } })
  expect(bash.reason).toBe(GUARD.reason)
})

test('leaves an allow or a deny verdict untouched', async ($, on) => {
  on('tool.check', () => ({ decision: 'allow' as const }))
  const allowed = await $.tool.check({ tool: 'Write', input: { file_path: PATH, content: note('description: x') } })
  expect(allowed.decision).toBe('allow')
})

test('leaves a Write outside any memory directory untouched', async ($, on) => {
  on('tool.check', () => ({ decision: 'ask' as const, reason: 'Other rule.' }))
  const out = await $.tool.check({ tool: 'Write', input: { file_path: '/repo/x.md', content: note('description: x') } })
  expect(out.reason).toBe('Other rule.')
})

test('survives an input without a path or content', async ($, on) => {
  on('tool.check', () => GUARD)
  const out = await $.tool.check({ tool: 'Write', input: {} })
  expect(out.reason).toBe(GUARD.reason)
})
