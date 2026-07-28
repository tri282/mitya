<script setup lang="ts">
// Phase 7 (composer-filter-debug): per-answer diagnostics from the `meta`
// event (design §III.9). Collapsed by default -- a single muted summary
// line; expands on click or focus-visible+Enter to the full seven-field
// set. `meta` is emitted LAST in the stream (design §IV, C1), so this
// phase's own Step 5 text (taking precedence over the draft design's
// "hidden while streaming" language for the identical gap -- see
// phase-7.md log) shows "diagnostics unavailable" for the whole window
// before it arrives, not a hidden/blank row -- this doubles as the
// graceful-degradation path for a hypothetical older backend that never
// emits `meta` at all.

import { ref } from "vue"
import type { MetaEvent } from "../api/types"

const props = defineProps<{
  meta?: MetaEvent
}>()

const expanded = ref(false)

function toggle(): void {
  if (!props.meta) return
  expanded.value = !expanded.value
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Enter") {
    e.preventDefault()
    toggle()
  }
}
</script>

<template>
  <div
    class="debug-row"
    :class="{ 'debug-row--expanded': expanded }"
    :tabindex="meta ? 0 : -1"
    role="button"
    :aria-expanded="meta ? expanded : undefined"
    @click="toggle"
    @keydown="onKeydown"
  >
    <p v-if="!meta" class="debug-row__summary debug-row__summary--unavailable">
      diagnostics unavailable
    </p>
    <template v-else>
      <p class="debug-row__summary">
        {{ meta.route_name }} · {{ meta.chunk_ids.length }} chunks · {{ meta.total_ms }} ms
      </p>
      <!-- Monospace only inside this expanded panel, never in the reading
           column (design §III.9) -- styling is Phase 8, this is structural. -->
      <dl v-if="expanded" class="debug-row__details">
        <dt>route_name</dt>
        <dd>{{ meta.route_name }}</dd>
        <dt>chunk_ids</dt>
        <dd>{{ meta.chunk_ids.join(", ") || "—" }}</dd>
        <dt>ttft_ms</dt>
        <dd>{{ meta.ttft_ms ?? "—" }}</dd>
        <dt>total_ms</dt>
        <dd>{{ meta.total_ms }}</dd>
        <dt>filter_relaxed</dt>
        <dd>{{ meta.filter_relaxed }}</dd>
        <dt>no_context</dt>
        <dd>{{ meta.no_context }}</dd>
        <dt>cache_hit</dt>
        <dd>{{ meta.cache_hit }}</dd>
      </dl>
    </template>
  </div>
</template>

<style scoped>
.debug-row {
  margin-top: var(--sp-4);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.debug-row__summary,
.debug-row__summary--unavailable {
  font-family: var(--font-chrome);
  font-size: var(--fs-meta);
  color: var(--ink-muted);
}

.debug-row__details {
  margin-top: var(--sp-3);
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--sp-1) var(--sp-3);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: var(--fs-meta);
  color: var(--ink-muted);
}

.debug-row__details dt {
  color: var(--ink-muted);
}

.debug-row__details dd {
  color: var(--ink);
  word-break: break-word;
}
</style>
