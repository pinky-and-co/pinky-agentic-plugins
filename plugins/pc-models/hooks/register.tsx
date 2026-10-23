// The plugin's one hooks module: each mod of pc-models lives in its own concern folder and is registered here.
import type { Register } from 'claude-code'

import { register as piView } from './pi-view/register'

export const register: Register = (on, options) => {
  piView(on, options)
}
