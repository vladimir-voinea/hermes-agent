#!/usr/bin/env node
// Bundle a demo .tsx (same @hermes/ink alias as build.mjs) and run it, so v2
// components that import '@hermes/ink' can be exercised headlessly for
// layout verification. Usage: node scripts/preview.mjs scripts/v2-demo.tsx
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const entry = resolve(process.cwd(), process.argv[2] ?? 'scripts/v2-demo.tsx')
const out = resolve(mkdtempSync(resolve(tmpdir(), 'v2prev-')), 'demo.mjs')

await build({
  entryPoints: [entry],
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node20',
  outfile: out,
  jsx: 'automatic',
  jsxImportSource: 'react',
  alias: { '@hermes/ink': resolve(root, 'packages/hermes-ink/src/entry-exports.ts') },
  banner: { js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);" },
  logLevel: 'error'
})

await import(pathToFileURL(out).href)
