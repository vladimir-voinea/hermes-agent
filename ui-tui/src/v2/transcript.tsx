/** @jsxRuntime automatic @jsxImportSource react */
// V2Transcript — flat/borderless, opencode-style transcript renderer.
//
// Presentational only: takes the plain `Msg[]` the existing gateway/state
// layer already produces and lays it out with NO boxes/borders anywhere.
// Vertical rhythm is the whole trick here: big air between turns (a new
// user message, a new assistant block, a new tool group, a new diff),
// tight spacing *within* a turn (consecutive tool rows, consecutive diff
// lines). See the v2 view spec this was built against for the exact
// per-role/kind rules encoded below.
import type { ReactNode } from 'react'

import { Box, Text } from '@hermes/ink'
import type { Msg } from '../types.js'

import { P } from './palette.js'

/** First line of a (possibly multi-line) thinking blob, trimmed. */
function firstLineOf(thinking: string): string {
  const line = thinking.split('\n')[0] ?? ''
  return line.trim()
}

/** Split a tool string's arg tail into { base, add, del }.
 *  "hermes_cli/_parser.py +7"       -> { base: 'hermes_cli/_parser.py', add: '+7', del: null }
 *  "ui-tui/src/entry.tsx +4 -1"     -> { base: 'ui-tui/src/entry.tsx',  add: '+4', del: '-1' }
 *  "some/path.ts"                   -> { base: 'some/path.ts',         add: null, del: null } */
function parseToolArgs(args: string): { add: null | string; base: string; del: null | string } {
  const tokens = args.split(' ').filter(Boolean)
  let del: null | string = null
  let add: null | string = null

  if (tokens.length > 0 && /^-\d+$/.test(tokens[tokens.length - 1]!)) {
    del = tokens.pop()!
  }

  if (tokens.length > 0 && /^\+\d+$/.test(tokens[tokens.length - 1]!)) {
    add = tokens.pop()!
  }

  return { add, base: tokens.join(' '), del }
}

/** One tool row: "› <name> <dim args> <green +N> <red -N>". */
function renderToolRow(toolStr: string, key: string, marginTop: number): ReactNode {
  const spaceIdx = toolStr.indexOf(' ')
  const name = spaceIdx === -1 ? toolStr : toolStr.slice(0, spaceIdx)
  const argsStr = spaceIdx === -1 ? '' : toolStr.slice(spaceIdx + 1)
  const { add, base, del } = parseToolArgs(argsStr)

  return (
    <Box key={key} marginTop={marginTop} paddingLeft={2}>
      <Text color={P.faint}>{'› '}</Text>
      <Text color={P.text}>{name}</Text>
      {base ? <Text color={P.dim}>{` ${base}`}</Text> : null}
      {add ? <Text color={P.green}>{` ${add}`}</Text> : null}
      {del ? <Text color={P.red}>{` ${del}`}</Text> : null}
    </Box>
  )
}

/** Color for one line of a unified diff. */
function diffLineColor(line: string) {
  if (line.startsWith('@@') || line.startsWith('diff') || line.startsWith('---') || line.startsWith('+++')) {
    return P.faint
  }

  if (line.startsWith('+')) {
    return P.green
  }

  if (line.startsWith('-')) {
    return P.red
  }

  return P.dim
}

function renderDiff(msg: Msg, i: number): ReactNode {
  const lines = msg.text.split('\n')

  return (
    <Box flexDirection="column" key={i} marginTop={1} paddingLeft={2}>
      {lines.map((line, li) => (
        <Text color={diffLineColor(line)} key={li}>
          {line}
        </Text>
      ))}
    </Box>
  )
}

function renderTools(msg: Msg, i: number): ReactNode {
  const tools = msg.tools ?? []
  return tools.map((toolStr, ti) => renderToolRow(toolStr, `${i}-${ti}`, ti === 0 ? 1 : 0))
}

function renderUser(msg: Msg, i: number): ReactNode {
  // A new user message starts a new turn — give it 2 blank rows of air so
  // turn boundaries read distinctly against the 1-row spacing used *within*
  // a turn (assistant / tool / diff blocks). First item hugs the top.
  return (
    <Box key={i} marginTop={i === 0 ? 0 : 2}>
      <Text bold color={P.accent}>
        {'❯ '}
      </Text>
      <Text color={P.primary}>{msg.text}</Text>
    </Box>
  )
}

function renderAssistant(msg: Msg, i: number): ReactNode {
  const textBox = msg.text ? (
    <Box key={i} marginTop={1} paddingLeft={2}>
      <Text color={P.body}>{msg.text}</Text>
    </Box>
  ) : null

  if (!msg.thinking) {
    return textBox
  }

  const thoughtBox = (
    <Box key={`${i}-thought`} marginTop={1} paddingLeft={2}>
      <Text color={P.faint} italic>
        {`thought · ${firstLineOf(msg.thinking)}`}
      </Text>
    </Box>
  )

  return [thoughtBox, textBox]
}

function renderSystem(msg: Msg, i: number): ReactNode {
  return (
    <Box key={i} marginTop={1} paddingLeft={2}>
      <Text color={P.dim}>{msg.text}</Text>
    </Box>
  )
}

function renderItem(msg: Msg, i: number): ReactNode {
  if (msg.kind === 'intro') {
    return null
  }

  if (msg.kind === 'diff') {
    return renderDiff(msg, i)
  }

  if (msg.tools?.length) {
    return renderTools(msg, i)
  }

  if (msg.role === 'user') {
    return renderUser(msg, i)
  }

  if (msg.role === 'assistant') {
    return renderAssistant(msg, i)
  }

  if (msg.role === 'system') {
    return renderSystem(msg, i)
  }

  // role === 'tool' with no tools[] payload — fall back to a plain dim
  // line rather than dropping the message on the floor.
  return renderSystem(msg, i)
}

export function V2Transcript({ items, width }: { items: Msg[]; width: number }) {
  return (
    <Box flexDirection="column" width={width}>
      {items.map((msg, i) => renderItem(msg, i))}
    </Box>
  )
}
