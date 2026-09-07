// Phase 5 (state-composables): polls GET /health for the HealthIndicator.
// Event-driven only -- poll once on mount, re-poll on demand after a /chat
// 503/504. No interval-based background timer of any kind (DECIDE C7).

import { onMounted, ref } from "vue"
import { getHealth } from "../api/client"
import type { Health } from "../api/types"

// Module-level singleton: state lives here (not inside useHealth()) so every
// caller -- AppHeader's HealthIndicator and useChat's 503/504 branches --
// shares one status instead of drifting independent copies.
const status = ref<"ok" | "degraded" | "unknown">("unknown")

// Phase 7 addition: retain the last full /health body so HealthIndicator's
// degraded tooltip can show the actual generation/qdrant subsystem detail
// (mitya-v2-ui Phase 3: it was llama_server until A3 made that line say only
// "not serving generation"; the tooltip now names the serving arm)
// (design §III.2) instead of just the collapsed status string. Not polled on
// an interval -- still populated only by the same mount-time/post-503-504
// poll() calls as `status` (DECIDE C7).
const detail = ref<Health | null>(null)

async function poll(): Promise<void> {
  try {
    const health = await getHealth()
    status.value = health.status
    detail.value = health
  } catch {
    status.value = "unknown"
    detail.value = null
  }
}

/** Exported directly (not only via useHealth()) so useChat can re-poll after
 * a 503/504 without instantiating its own disconnected composable state.
 * Always refetches -- that is the point of an on-demand re-poll. */
export const repoll = poll

// The state above is shared, but onMounted() registers per *calling component*
// -- three components calling useHealth() fired three concurrent /health
// requests on load. The mount-time poll is therefore once per module, not once
// per caller (C7: exactly one request per event, no redundant fan-out).
let mountPolled = false

export function useHealth() {
  onMounted(() => {
    if (mountPolled) return
    mountPolled = true
    void poll()
  })
  return { status, detail, repoll: poll }
}
