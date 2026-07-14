/** @jsxRuntime automatic @jsxImportSource react */
// Standalone self-verification for V2Transcript — no TTY needed. Renders a
// realistic sample transcript to an offscreen Screen buffer (via the same
// renderToScreen() used for search) and prints it as a plain text grid.
//
// NOTE: cellAt() only recovers `.char`, not style/color — this verifies
// LAYOUT ONLY (spacing, wrapping, indentation, blank-line grouping between
// turns/blocks). Colors must be eyeballed by reading transcript.tsx.
//
// Run: npx tsx scripts/v2-demo.tsx
import { cellAt } from '../packages/hermes-ink/src/ink/screen.js'
import { renderToScreen } from '../packages/hermes-ink/src/ink/render-to-screen.js'
import { V2Transcript } from '../src/v2/transcript.js'
import type { Msg } from '../src/types.js'

const sample: Msg[] = [
  { role: 'user', text: 'Can you fix the frame counter bug in the parser?' },
  {
    role: 'assistant',
    text: 'Sure — let me look at the parser and the entry point that wires it up.',
    thinking: 'Need to check hermes_cli/_parser.py and ui-tui/src/entry.tsx for the counter logic.'
  },
  {
    role: 'tool',
    text: '',
    tools: ['edit hermes_cli/_parser.py +7', 'edit ui-tui/src/entry.tsx +4 -1']
  },
  {
    role: 'assistant',
    kind: 'diff',
    text: [
      'diff --git a/hermes_cli/_parser.py b/hermes_cli/_parser.py',
      '--- a/hermes_cli/_parser.py',
      '+++ b/hermes_cli/_parser.py',
      '@@ -12,7 +12,7 @@ def parse_frame(buf):',
      '-    count = 0',
      '+    count = 1',
      '     for chunk in buf:',
      '+        count += 1',
      '         yield chunk'
    ].join('\n')
  },
  { role: 'assistant', text: 'Fixed — the counter now starts at 1 and increments per chunk. Tests pass.' },
  { role: 'user', text: 'Great, can you also add a changelog entry?' },
  { role: 'assistant', text: 'Added a changelog entry under Unreleased.' }
]

const W = 92
const { screen, height } = renderToScreen(<V2Transcript items={sample} width={W} />, W)

let out = ''
for (let y = 0; y < height; y++) {
  let ln = ''
  for (let x = 0; x < W; x++) {
    ln += (cellAt(screen, x, y)?.char ?? ' ') || ' '
  }
  out += ln.replace(/\s+$/, '') + '\n'
}

process.stdout.write(out)
