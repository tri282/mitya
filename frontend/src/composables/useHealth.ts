// Phase 5 (state-composables): polls GET /health for the HealthIndicator.
// Event-driven only -- poll once on mount, re-poll on demand after a /chat
// 503/504. No interval-based background timer of any kind (DECIDE C7).

import { onMounted, ref } from "vue"
import { getHealth } from "../api/client"

// Module-level singleton: state lives here (not inside useHealth()) so every
// caller -- AppHeader's HealthIndicator and useChat's 503/504 branches --
// shares one status instead of drifting independent copies.
const status = ref<"ok" | "degraded" | "unknown">("unknown")

async function poll(): Promise<void> {
  try {
    const health = await getHealth()
    status.value = health.status
  } catch {
    status.value = "unknown"
  }
}

/** Exported directly (not only via useHealth()) so useChat can re-poll after
 * a 503/504 without instantiating its own disconnected composable state. */
export const repoll = poll

export function useHealth() {
  onMounted(poll)
  return { status, repoll: poll }
}
