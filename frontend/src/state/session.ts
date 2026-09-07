// Phase 5 (state-composables): client-side transcript + localStorage persistence.
// The transcript is UI-only state -- never sent back to /chat (design §II.3).
//
// v2 Phase 5 (session-persistence, design §3.4): both storage keys are now
// tenant-scoped and a single module-scope watcher on `tenant` (state/auth.ts)
// covers login, logout, 401 expiry and tenant switch uniformly.

import { reactive, ref, watch } from "vue"
import type { ChatFilters, MetaEvent, Source } from "../api/types"
import { tenant } from "./auth"

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

const SESSION_PREFIX = "mitya.session.v1."
const TRANSCRIPT_PREFIX = "mitya.transcript.v2." // was: mitya.transcript.v1, tenant-less
const LEGACY_TRANSCRIPT_KEY = "mitya.transcript.v1"
const SOFT_CAP = 50
const DEBOUNCE_MS = 200
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// Keys are only ever BUILT from prefix + tenant id, never parsed, so no
// delimiter in a tenant id can create ambiguity.
const sessionKey = (t: string) => SESSION_PREFIX + t
const transcriptKey = (t: string) => TRANSCRIPT_PREFIX + t

// Empty until a tenant is adopted (design §3.4, index Gap 2: was a plain
// string minted at module load). useChat.ts reads `.value`.
export const sessionId = ref("")

function loadTurns(key: string): Turn[] {
  // The ACCESSOR itself throws in some contexts (blocked site data, some
  // private-browsing modes), not just the value being absent -- so the read is
  // wrapped, not merely null-checked.
  let raw: string | null
  try {
    raw = localStorage.getItem(key)
  } catch {
    return []
  }
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
  turns: [] as Turn[],
})

let currentTenant: string | null = null
let debounceHandle: ReturnType<typeof setTimeout> | null = null

/** Serialize the soft-capped tail of the transcript, dropping the oldest
 * turn on QuotaExceededError and retrying until it fits or the array is
 * empty. Never throws, never blocks the caller (design §II.3, DECIDE C4).
 * No-ops when no tenant is adopted -- there is no tenant-scoped key to
 * write to. */
function persist(): void {
  if (currentTenant === null) return
  const key = transcriptKey(currentTenant)
  let toStore = session.turns.slice(-SOFT_CAP)
  while (true) {
    try {
      localStorage.setItem(key, JSON.stringify(toStore))
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

/** Adopt tenant `t`: mint or reuse its session id, replace the in-memory
 * transcript IN PLACE (design §3.4 reactivity trap -- useChat.ts:125 returns
 * the array reference, captured once at App.vue:44; reassigning
 * `session.turns` would leave that reference pointing at the old array). */
function adopt(t: string): void {
  // FIRST, before any storage call can throw. `persist()` keys its write on
  // `currentTenant`, so leaving it pointing at the PREVIOUS tenant while
  // `tenant` has already moved would write this tenant's turns under the
  // previous tenant's key -- the one thing §6's key-namespace rule promises
  // cannot happen. A storage failure must degrade to "in memory only, correct
  // tenant", never to "persisted under the wrong tenant".
  currentTenant = t
  let stored: string | null = null
  try {
    stored = localStorage.getItem(sessionKey(t))
  } catch {
    // Storage unreadable: fall through to a freshly minted id. It will not
    // survive the reload, which is exactly the pre-Phase-5 behaviour and the
    // honest degradation when there is nowhere to persist to.
  }
  sessionId.value = UUID_RE.test(stored ?? "") ? (stored as string) : crypto.randomUUID()
  // Separately guarded, same reason as purge(): a failed write must not skip
  // the residue sweep below it.
  try {
    if (sessionId.value !== stored) localStorage.setItem(sessionKey(t), sessionId.value)
  } catch {
    /* no persistence available; the minted id is in-memory only */
  }
  try {
    // One-line sweep of the pre-tenant-scoped key (design §2's residue).
    localStorage.removeItem(LEGACY_TRANSCRIPT_KEY)
  } catch {
    /* best effort */
  }
  // IN PLACE -- never `session.turns = ...`. loadTurns guards its own read.
  session.turns.splice(0, session.turns.length, ...loadTurns(transcriptKey(t)))
}

/** Purge both tenant-scoped keys and clear in-memory state. Called from
 * `auth.ts`'s `logout()` BEFORE `tenant.value` is cleared, while the tenant
 * id is still known. Must never throw -- storage failure must never block
 * sign-out (design §3.4, §6). */
export function purge(): void {
  const t = currentTenant
  currentTenant = null
  sessionId.value = ""
  session.turns.splice(0, session.turns.length)
  if (t === null) return
  // Each removal is guarded SEPARATELY, not as a pair: a throw on the first
  // must not skip the second. The transcript is the more sensitive of the two
  // -- it holds answer text, where the session key holds only a partition id
  // -- so a shared try block would leave exactly the wrong thing on disk.
  // Storage disabled / private mode: a cleanup failure must NEVER block
  // sign-out. The auth transition is the security control; this is not.
  try {
    localStorage.removeItem(sessionKey(t))
  } catch {
    /* keep going -- the transcript below matters more */
  }
  try {
    localStorage.removeItem(transcriptKey(t))
  } catch {
    /* nothing further to attempt */
  }
}

// One watcher covers login, logout, 401 expiry and tenant switch uniformly
// -- there is no second place to keep in step (design §3.4).
watch(tenant, (next) => {
  if (next === null) return // sign-out / expiry: in-memory state left alone, nothing written (rule 3)
  if (next === currentTenant) return // 401-then-sign-back-in: no-op, transcript survives (rule 2)
  adopt(next) // rule 1: real tenant change
})
