// The plugin's one hooks module: each mod of pc-core lives in its own concern folder and is registered here.
import type { Register } from 'claude-code'

import { register as piView } from './pi-view/register'
import { register as subagentRouting } from './subagent-routing/register'

export const register: Register = (on, options) => {
  subagentRouting(on, options)
  piView(on, options)
}
