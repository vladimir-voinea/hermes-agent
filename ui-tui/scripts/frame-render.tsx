/** @jsxRuntime automatic @jsxImportSource react */
import { render } from '@hermes/ink'
import { V2Transcript } from '../src/v2/transcript.js'
import type { Msg } from '../src/types.js'
const sample: Msg[] = [
  { role:'user', text:'add a --tui2 flag that launches the v2 view' },
  { role:'assistant', text:"I'll wire --tui2 through the parser; both views ship in one dist/entry.js.", thinking:'planning the parser + launch wiring' },
  { role:'tool', text:'', tools:['edit hermes_cli/_parser.py +7','edit ui-tui/src/entry.tsx +4 -1'] },
  { role:'assistant', kind:'diff', text:"@@ ui-tui/src/entry.tsx\n-  import('./app.js'),\n+  variant === '2' ? v2App() : app()," },
  { role:'assistant', text:'Done — --tui stays byte-identical; --tui2 boots the neutral palette.' },
  { role:'user', text:'now make it beautiful' },
  { role:'assistant', text:'On it.' }
]
const inst = render(<V2Transcript items={sample} width={92} />)
setTimeout(() => { try { inst.unmount() } catch {}; process.exit(0) }, 250)
