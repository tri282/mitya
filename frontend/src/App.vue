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
const { turns, send } = useChat()

// Prepared for Phase 7's Composer, which isn't functional yet (no props/
// v-model on the current stub) -- EmptyState's example chips already need
// somewhere to land, so this state exists now and Phase 7 wires it into the
// real Composer's textarea/FilterControl v-model.
const composerDraft = ref("")
const composerFilters = ref<ChatFilters>({})

function onRetry(turn: Turn): void {
  void send(turn.question, turn.filters)
}

function onExampleSelect(payload: { query: string; book: string }): void {
  composerDraft.value = payload.query
  composerFilters.value = { book: payload.book }
}
</script>

<template>
  <div class="app-shell">
    <AppHeader />
    <Transcript :turns="turns" @retry="onRetry" @example-select="onExampleSelect" />
    <Composer />
  </div>
</template>
