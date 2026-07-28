// Phase 5 (state-composables): drives POST /chat via chatStream, turn state machine.

import { ref } from "vue"
import { ChatHttpError, streamChat } from "../api/chatStream"
import type { ChatFilters } from "../api/types"
import { scheduleSave, session, sessionId, type Turn } from "../state/session"
import { repoll } from "./useHealth"

export function useChat() {
  // Single source of truth for the busy state (design §II.4) -- Composer,
  // AppHeader, and Transcript all read this, never local component state.
  const isStreaming = ref(false)
  let activeController: AbortController | null = null

  async function send(query: string, filters?: ChatFilters): Promise<void> {
    // Enforcement point for "no client queue" (DECIDE C6): the composable
    // itself no-ops a send while a turn is already streaming, rather than
    // relying solely on the caller to gate it. Phase 7's Composer disables
    // its submit control from the same isStreaming flag for UX, but this is
    // the contract-enforcing guard.
    if (isStreaming.value) return

    const turn: Turn = {
      id: crypto.randomUUID(),
      question: query,
      filters,
      answer: "",
      sources: [],
      footer: "",
      state: "streaming",
      createdAt: Date.now(),
    }
    session.turns.push(turn)
    scheduleSave()
    isStreaming.value = true
    const controller = new AbortController()
    activeController = controller

    try {
      for await (const ev of streamChat({ query, filters, session_id: sessionId }, controller.signal)) {
        switch (ev.kind) {
          case "token":
            turn.answer += ev.data.text
            break
          case "sources":
            turn.sources = ev.data.sources
            turn.footer = ev.data.footer
            break
          case "meta":
            // Never gates prose/sources rendering (design §IV.2) -- just
            // stored for Phase 7's DebugRow. No no_context computation here
            // (DECIDE C11) -- that is a rendering decision off sources.length,
            // made by SourcesList (Phase 6).
            turn.meta = ev.data
            break
          case "error":
            turn.state = "stream-error"
            turn.errorDetail = ev.data.detail
            scheduleSave()
            return
          case "done":
            // Cache-path terminal marker; live path has no `done` event
            // (see phase-4.md) and instead falls through to stream close
            // below. Either way turn.state is set to "complete" after the
            // loop.
            break
        }
      }
      turn.state = "complete"
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        // User-initiated Stop (Phase 7) -- tokens received so far are kept
        // verbatim, turn is never re-submitted automatically.
        turn.state = "interrupted"
      } else if (e instanceof ChatHttpError) {
        turn.state = `error-${e.status}` as Turn["state"]
        turn.errorDetail = e.detail
        // Re-poll health on the two signals that the backend state may have
        // changed (DECIDE C7) -- shares useHealth's singleton status so
        // HealthIndicator reflects it without a separate instance.
        if (e.status === 503 || e.status === 504) void repoll()
      } else {
        // Some other network failure -- still reach a terminal state, never
        // hang.
        turn.state = "stream-error"
        turn.errorDetail = e instanceof Error ? e.message : String(e)
      }
    } finally {
      isStreaming.value = false
      activeController = null
      scheduleSave()
    }
  }

  function stop(): void {
    // Aborts the fetch; the AbortError branch above sets state to
    // "interrupted". No separate cache write to guard client-side -- the
    // server's cache.set only runs after the token loop completes without
    // cancellation (verified against app.py; see plan.md Open Gap #5), so a
    // real client disconnect already prevents a partial-answer cache write
    // server-side.
    activeController?.abort()
  }

  return { turns: session.turns, isStreaming, send, stop }
}
