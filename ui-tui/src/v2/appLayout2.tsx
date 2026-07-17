import { AlternateScreen, Box, ScrollBox } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { Fragment, memo } from 'react'

import type { AppLayoutProps } from '../app/interfaces.js'
import { $overlayState } from '../app/overlayStore.js'
import { $uiState } from '../app/uiStore.js'
import {
  AgentsOverlayPane,
  ComposerPane,
  JourneyPane,
  PetPane
} from '../components/appLayout.js'
import { PromptZone } from '../components/appOverlays.js'
import { StreamingAssistant } from '../components/streamingAssistant.js'
import { INLINE_MODE } from '../config/env.js'
import { PerfPane } from '../lib/perfPane.js'

import { V2Transcript } from './transcript.js'

// ── v2 transcript pane ───────────────────────────────────────────────
// The ONLY thing v2 changes vs the classic layout: the message renderer.
// Everything below (composer, prompt zone, overlays, pet, status rule) is
// reused verbatim from the classic layout — it is not the "ugly" part, and
// the OPENCODE_THEME palette already neutralizes it. Here we drop the boxed
// MessageLine/Banner rendering for the flat, borderless V2Transcript.
const V2TranscriptPane = memo(function V2TranscriptPane({
  actions,
  composer,
  progress,
  transcript
}: Pick<AppLayoutProps, 'actions' | 'composer' | 'progress' | 'transcript'>) {
  const ui = useStore($uiState)
  const bodyCols = Math.max(1, composer.cols - 2)

  return (
    <ScrollBox
      flexDirection="column"
      flexGrow={1}
      flexShrink={1}
      onClick={(e: { cellIsBlank?: boolean }) => {
        if (e.cellIsBlank) {
          actions.clearSelection()
        }
      }}
      ref={transcript.scrollRef}
      stickyScroll
    >
      <Box flexDirection="column" paddingX={1}>
        <V2Transcript items={transcript.historyItems} width={bodyCols} />

        <StreamingAssistant
          cols={bodyCols}
          compact={ui.compact}
          detailsMode={ui.detailsMode}
          detailsModeCommandOverride={ui.detailsModeCommandOverride}
          prevMsg={transcript.historyItems[transcript.historyItems.length - 1]}
          progress={progress}
          sections={ui.sections}
        />
      </Box>
    </ScrollBox>
  )
})

// ── v2 root layout ───────────────────────────────────────────────────
// Mirrors the classic AppLayout composition exactly (agents/journey overlay
// swap, prompt zone + composer at the bottom, floating pet) but renders the
// flat V2TranscriptPane in the transcript slot.
export const AppLayout2 = memo(function AppLayout2({
  actions,
  composer,
  mouseTracking,
  progress,
  status,
  transcript
}: AppLayoutProps) {
  const overlay = useStore($overlayState)

  const Shell = INLINE_MODE ? Fragment : AlternateScreen
  const shellProps = INLINE_MODE ? {} : { mouseTracking }

  return (
    <Shell {...shellProps}>
      <Box flexDirection="column" flexGrow={1} position="relative">
        <Box flexDirection="row" flexGrow={1}>
          {overlay.agents ? (
            <PerfPane id="agents">
              <AgentsOverlayPane />
            </PerfPane>
          ) : overlay.journey ? (
            <PerfPane id="journey">
              <JourneyPane />
            </PerfPane>
          ) : (
            <PerfPane id="transcript">
              <V2TranscriptPane actions={actions} composer={composer} progress={progress} transcript={transcript} />
            </PerfPane>
          )}
        </Box>

        {!overlay.agents && !overlay.journey && (
          <>
            <PerfPane id="prompt">
              <PromptZone
                cols={composer.cols}
                onApprovalChoice={actions.answerApproval}
                onClarifyAnswer={actions.answerClarify}
                onSecretSubmit={actions.answerSecret}
                onSudoSubmit={actions.answerSudo}
              />
            </PerfPane>

            <PerfPane id="composer">
              <ComposerPane actions={actions} composer={composer} status={status} />
            </PerfPane>
          </>
        )}

        {!overlay.agents && <PetPane />}
      </Box>
    </Shell>
  )
})
