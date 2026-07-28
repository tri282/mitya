<script setup lang="ts">
// Phase 7 (composer-filter-debug): static shell + HealthIndicator (design
// §III.1). No props, no nav, no actions. Calls the shared useHealth()
// singleton directly -- same direct-read pattern Phase 6 established for
// useBooks() (QuestionLine/SourcesList) -- so HealthIndicator's `status`
// prop is populated without App.vue threading it through a second time (see
// App.vue's own Phase 6 note: its useHealth() call only exists to trigger
// the mount-time poll).
import HealthIndicator from "./HealthIndicator.vue"
import { useHealth } from "../composables/useHealth"

const { status } = useHealth()
</script>

<template>
  <header class="app-header">
    <h1 class="app-header__title">Mitya</h1>
    <HealthIndicator :status="status" />
  </header>
</template>

<style scoped>
.app-header {
  position: sticky;
  top: 0;
  z-index: var(--z-header);
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-6);
  background: var(--surface);
}

.app-header__title {
  font-family: var(--font-reading);
  font-weight: 500;
  font-size: var(--fs-display);
  color: var(--ink);
}
</style>
