import { useStore } from '@nanostores/react'

import { GatewayProvider } from '../app/gatewayContext.js'
import { $uiState } from '../app/uiStore.js'
import { useMainApp } from '../app/useMainApp.js'
import type { GatewayClient } from '../gatewayClient.js'

import { AppLayout2 } from './appLayout2.js'

// ── Hermes TUI v2 (--tui2) ───────────────────────────────────────────
// An opencode-style, flat/borderless reimagining of the terminal UI. It
// reuses the entire gateway + state layer (`useMainApp`) — every CLI feature
// is inherited for free — and only swaps the VIEW: a flat, borderless
// transcript (V2Transcript) in place of the boxed classic renderer, on the
// neutral OPENCODE_THEME palette. Composer, overlays, prompts and pet are the
// classic components, reused verbatim (they aren't the "ugly" part).
export function App({ gw }: { gw: GatewayClient }) {
  const { appActions, appComposer, appProgress, appStatus, appTranscript, gateway } = useMainApp(gw)
  const { mouseTracking } = useStore($uiState)

  return (
    <GatewayProvider value={gateway}>
      <AppLayout2
        actions={appActions}
        composer={appComposer}
        mouseTracking={mouseTracking}
        progress={appProgress}
        status={appStatus}
        transcript={appTranscript}
      />
    </GatewayProvider>
  )
}
