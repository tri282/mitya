<script setup lang="ts">
// Phase 7 (composer-filter-debug): subtle backend status (design §III.2,
// DECIDE C7). Three states only, no interval poll anywhere in this file --
// `status` arrives as a prop (mount-time + post-503/504 poll, driven by
// useHealth()/useChat() elsewhere). Never a red banner -- this is a reading
// room, not a dashboard.

import { computed } from "vue"
import { useHealth } from "../composables/useHealth"

const props = defineProps<{
  status: "ok" | "degraded" | "unknown"
}>()

// Accessible label per state. "Mitya's study is dimmed" (degraded) and
// "checking…" (unknown) are the design's verbatim copy (§III.2); "ok" has
// no verbatim copy in the spec, so a plain, non-voiced label is used rather
// than inventing product copy (judgment call, see phase-7.md log).
const LABELS: Record<"ok" | "degraded" | "unknown", string> = {
  ok: "Mitya's study is lit",
  degraded: "Mitya's study is dimmed",
  unknown: "checking…",
}
const label = computed(() => LABELS[props.status])

// Only the degraded state carries a tooltip with the real /health subsystem
// detail (design §III.2) -- read off the shared useHealth() singleton's
// `detail` ref (the last-fetched Health body), not a second prop, so this
// component's boundary stays `{ status }` per the design (same direct-read-
// beyond-props pattern QuestionLine/SourcesList use for useBooks()).
const { detail } = useHealth()

const subsystemDetail = computed(() => {
  const health = detail.value
  if (!health) return null
  const qdrant =
    typeof health.qdrant === "string"
      ? health.qdrant
      : `${health.qdrant.collection} (${health.qdrant.count})`
  return `llama_server: ${health.llama_server} · qdrant: ${qdrant}`
})
</script>

<template>
  <div
    class="health-indicator"
    :class="`health-indicator--${status}`"
    tabindex="0"
    role="status"
    :aria-label="label"
    :aria-describedby="status === 'degraded' ? 'health-indicator-tooltip' : undefined"
  >
    <span class="health-indicator__dot" aria-hidden="true"></span>
    <span
      v-if="status === 'degraded'"
      id="health-indicator-tooltip"
      role="tooltip"
      class="health-indicator__tooltip"
    >
      {{ label }}<template v-if="subsystemDetail"> — {{ subsystemDetail }}</template>
    </span>
  </div>
</template>

<style scoped>
/* Tooltip is revealed on hover and on focus-visible (keyboard users), never
   only on hover (design §III.2). Dot color carries the only amber use in
   this component -- the "degraded" state -- which the design's own
   component spec (§III.2) calls for explicitly as a status affordance, not
   a decorative one. */
.health-indicator {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  border-radius: var(--radius-sm);
  cursor: default;
}

.health-indicator__dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-sm);
  background: var(--ink-muted);
  transition: background-color var(--dur-fast) var(--ease-out-exp);
}

.health-indicator--degraded .health-indicator__dot {
  background: var(--accent);
}

.health-indicator--unknown .health-indicator__dot {
  background: transparent;
  border: 1px solid var(--ink-muted);
}

.health-indicator__tooltip {
  position: absolute;
  top: calc(100% + var(--sp-2));
  right: 0;
  z-index: var(--z-tooltip);
  visibility: hidden;
  opacity: 0;
  min-width: 220px;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  color: var(--ink-muted);
  font-size: var(--fs-meta);
  line-height: 1.4;
  white-space: normal;
  transition:
    opacity var(--dur-fast) var(--ease-out-exp),
    visibility var(--dur-fast);
}

.health-indicator:hover .health-indicator__tooltip,
.health-indicator:focus-visible .health-indicator__tooltip {
  visibility: visible;
  opacity: 1;
}
</style>
