// Phase 5 (state-composables): client-side transcript + localStorage persistence.
// The transcript is UI-only state -- never sent back to /chat (design §II.3).

import { reactive } from "vue"
import type { ChatFilters, MetaEvent, Source } from "../api/types"

// Turn, ported verbatim from design §II.3 + the user's stream-cancel
// resolution (Open Gap #5): "interrupted" added to the state union for a
// turn ended by the Phase 7 Stop control mid-stream.
export interface Turn {
  id: string
  question: string
  filters?: ChatFilters
  answer: string
  sources: Source[]
  footer: string
  meta?: MetaEvent
  state:
    | "streaming"
    | "complete"
    | "stream-error"
    | "error-400"
    | "error-401"
    | "error-429"
    | "error-503"
    | "error-504"
    | "interrupted"
  errorDetail?: string
  createdAt: number
}

const STORAGE_KEY = "mitya.transcript.v1"
const SOFT_CAP = 50
const DEBOUNCE_MS = 200

// One session id per page load/reload (module loads exactly once per load).
export const sessionId = crypto.randomUUID()

function loadTurns(): Turn[] {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (raw === null) return []
  try {
    const parsed = JSON.parse(raw) as unknown
    return Array.isArray(parsed) ? (parsed as Turn[]) : []
  } catch {
    // Corrupt JSON -- discard and start empty, never crash on load.
    return []
  }
}

export const session = reactive({
  turns: loadTurns(),
})

let debounceHandle: ReturnType<typeof setTimeout> | null = null

/** Serialize the soft-capped tail of the transcript, dropping the oldest
 * turn on QuotaExceededError and retrying until it fits or the array is
 * empty. Never throws, never blocks the caller (design §II.3, DECIDE C4). */
function persist(): void {
  let toStore = session.turns.slice(-SOFT_CAP)
  while (true) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(toStore))
      return
    } catch (e) {
      if (e instanceof DOMException && e.name === "QuotaExceededError" && toStore.length > 0) {
        toStore = toStore.slice(1)
        continue
      }
      // Any other failure, or an already-empty array: give up silently.
      // The in-memory reactive array is never trimmed by a failed write.
      return
    }
  }
}

/** Debounced (200ms) localStorage write. Call after every mutation to
 * session.turns that should be persisted. */
export function scheduleSave(): void {
  if (debounceHandle !== null) clearTimeout(debounceHandle)
  debounceHandle = setTimeout(persist, DEBOUNCE_MS)
}
