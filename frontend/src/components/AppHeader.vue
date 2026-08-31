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
import { logout, tenant } from "../state/auth"

const { status } = useHealth()

// v2 P5 step 7. The header is where the account lives because it is the only
// persistent chrome there is; the alternative was a settings surface, which
// D8 rules out. The tenant id IS the display name -- the server has no other
// notion of a user, and inventing one client-side would be a second identity.
function onSignOut(): void {
  void logout()
}
</script>

<template>
  <header class="app-header">
    <h1 class="app-header__title">Mitya</h1>
    <div class="app-header__right">
      <HealthIndicator :status="status" />
      <span v-if="tenant" class="app-header__tenant">{{ tenant }}</span>
      <button v-if="tenant" type="button" class="app-header__signout" @click="onSignOut">
        Sign out
      </button>
    </div>
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

.app-header__right {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
}

.app-header__tenant {
  font-family: var(--font-chrome);
  font-size: var(--fs-meta);
  color: var(--ink-muted);
}

.app-header__signout {
  padding: 0;
  font-family: var(--font-chrome);
  font-size: var(--fs-meta);
  color: var(--ink-muted);
  background: none;
  border: none;
  cursor: pointer;
}

.app-header__signout:hover {
  color: var(--ink);
}

.app-header__title {
  font-family: var(--font-reading);
  font-weight: 500;
  font-size: var(--fs-display);
  color: var(--ink);
}
</style>
