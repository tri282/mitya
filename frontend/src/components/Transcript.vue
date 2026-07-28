<script setup lang="ts">
// Phase 6 (transcript-components): the scroll region (design §III.3). Maps
// turns -> QuestionLine + AnswerBlock pairs; renders EmptyState when empty.
// Owns autoscroll (sticky-to-bottom, suppressed once the user scrolls up,
// re-armed on a new turn) and the aria-live announcement (turn completion
// only, never per token).

import { computed, nextTick, ref, watch } from "vue"
import type { Turn } from "../state/session"
import AnswerBlock from "./AnswerBlock.vue"
import EmptyState from "./EmptyState.vue"
import QuestionLine from "./QuestionLine.vue"

const props = defineProps<{
  turns: Turn[]
}>()

// Boundary deviation (flagged in phase-6.md log, same rationale as
// AnswerBlock): design §III.3 lists Transcript's boundary as "no events",
// but the working Try-again control (§VI) and EmptyState's example chips
// both need to reach App.vue's single useChat()/composer-draft state, so
// this component is a thin pass-through for both.
const emit = defineEmits<{
  retry: [turn: Turn]
  "example-select": [payload: { query: string; book: string }]
}>()

const scrollRegion = ref<HTMLElement | null>(null)
const userScrolledUp = ref(false)
const SCROLL_THRESHOLD_PX = 48

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - SCROLL_THRESHOLD_PX
}

function onScroll(): void {
  const el = scrollRegion.value
  if (!el) return
  userScrolledUp.value = !isNearBottom(el)
}

function scrollToBottom(): void {
  const el = scrollRegion.value
  if (!el) return
  el.scrollTop = el.scrollHeight
}

// Re-arm on every new turn: a fresh turn always starts pinned to bottom,
// regardless of where the reader had scrolled during the previous turn.
watch(
  () => props.turns.length,
  async () => {
    userScrolledUp.value = false
    await nextTick()
    scrollToBottom()
  },
)

// Stick to bottom while the last turn streams in, unless the reader has
// scrolled away from the bottom.
const lastTurn = computed<Turn | undefined>(() => props.turns[props.turns.length - 1])
watch(
  () => lastTurn.value?.answer,
  async () => {
    if (userScrolledUp.value) return
    await nextTick()
    scrollToBottom()
  },
)

// aria-live region: a visually-hidden element whose text only changes when
// the last turn reaches a terminal state -- NOT on every token append (which
// only mutates `.answer`, never `.state`). This is the sole aria-live region
// in the file (verify: grep for aria-live should show exactly this one spot,
// driven by a `state` watcher, not the scrolling content itself).
const announcement = ref("")
watch(
  () => lastTurn.value?.state,
  (state) => {
    if (!state || state === "streaming") return
    announcement.value = "Mitya finished answering."
  },
)

function onRetry(turn: Turn): void {
  emit("retry", turn)
}

function onExampleSelect(payload: { query: string; book: string }): void {
  emit("example-select", payload)
}
</script>

<template>
  <div class="transcript">
    <div ref="scrollRegion" class="transcript__scroll" @scroll="onScroll">
      <EmptyState v-if="turns.length === 0" @example-select="onExampleSelect" />
      <template v-else>
        <div v-for="turn in turns" :key="turn.id" class="transcript__turn">
          <QuestionLine :question="turn.question" :filters="turn.filters" />
          <AnswerBlock :turn="turn" @retry="onRetry(turn)" />
        </div>
      </template>
    </div>
    <div class="transcript__announcer" aria-live="polite" aria-atomic="true">
      {{ announcement }}
    </div>
  </div>
</template>

<style scoped>
.transcript {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.transcript__scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: var(--sp-6);
}

.transcript__turn + .transcript__turn {
  margin-top: var(--sp-7);
}

.transcript__announcer {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
</style>
