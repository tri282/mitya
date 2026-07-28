<script setup lang="ts">
// Phase 6 (transcript-components): layout shell (design §I.2/§I.3). Mounts
// AppHeader, Transcript, Composer. No business logic beyond wiring -- this
// file stays thin.
//
// Phase 7 note (read before wiring Composer/AppHeader/FilterControl): this
// is the ONE place useChat()/useHealth() are instantiated. Composer must
// receive `send`/`isStreaming`/`stop` as props/emits from here rather than
// calling useChat() itself -- a second useChat() call creates an independent
// `isStreaming` ref/AbortController, which would silently break the single
// in-flight guarantee (design §II.4) between the composer and the transcript
// retry path already wired below.

import { ref } from "vue"
import type { ChatFilters } from "./api/types"
import AppHeader from "./components/AppHeader.vue"
import Composer from "./components/Composer.vue"
import Transcript from "./components/Transcript.vue"
import { useChat } from "./composables/useChat"
import { useHealth } from "./composables/useHealth"
import type { Turn } from "./state/session"

// Shared useHealth() singleton (Phase 5 note 3) -- triggers its onMounted
// poll here; AppHeader's HealthIndicator (Phase 7) reads the same module-
// level `status` ref via its own useHealth() call, no threading needed.
useHealth()

// The single useChat() instance for the whole app (see note above).
const { turns, isStreaming, send, stop } = useChat()

// Composer's textarea/FilterControl v-models (Phase 6 note) -- EmptyState's
// example chips populate these, and Phase 7's Composer now consumes them.
const composerDraft = ref("")
const composerFilters = ref<ChatFilters>({})

function onRetry(turn: Turn): void {
  void send(turn.question, turn.filters)
}

function onExampleSelect(payload: { query: string; book: string }): void {
  composerDraft.value = payload.query
  composerFilters.value = { book: payload.book }
}

function onSubmit(payload: { query: string; filters: ChatFilters }): void {
  // Drop an empty filters object rather than send `{}` -- keeps the wire
  // payload identical to an unscoped query with no `filters` key at all
  // (types.ts's exclude_none note).
  const filters = Object.keys(payload.filters).length > 0 ? payload.filters : undefined
  void send(payload.query, filters)
}
</script>

<template>
  <div class="app-shell">
    <AppHeader />
    <Transcript :turns="turns" @retry="onRetry" @example-select="onExampleSelect" />
    <Composer
      :disabled="isStreaming"
      v-model:draft="composerDraft"
      v-model:filters="composerFilters"
      @submit="onSubmit"
      @stop="stop"
    />
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
  max-width: 100%;
}
</style>
