<script setup lang="ts">
// Phase 7 (composer-filter-debug): the textarea, FilterControl, and send
// (design §III.7). `App.vue` owns the single useChat() instance (see its
// own warning comment) -- this component receives `disabled` (bound to
// isStreaming) as a prop and emits `submit`/`stop` upward; it never calls
// useChat() itself, or a second isStreaming/AbortController would silently
// break the single in-flight guarantee (DECIDE C6).
//
// `draft`/`filters` are v-models threaded from App.vue's composerDraft/
// composerFilters (Phase 6 note) so EmptyState's example chips can populate
// this component without it owning that state itself.

import { computed } from "vue"
import type { ChatFilters } from "../api/types"
import { useBooks } from "../composables/useBooks"
import FilterControl from "./FilterControl.vue"

const props = defineProps<{
  disabled: boolean
  draft: string
  filters: ChatFilters
}>()

const emit = defineEmits<{
  submit: [payload: { query: string; filters: ChatFilters }]
  stop: []
  "update:draft": [value: string]
  "update:filters": [value: ChatFilters]
}>()

// FilterControl's error state (design §III.8): books fetch failed -> the
// control is hidden entirely, unscoped queries still work. Shared
// useBooks() singleton -- same tree/error the FilterControl below is fed.
const { tree, error } = useBooks()

const filtersModel = computed<ChatFilters>({
  get: () => props.filters,
  set: (value) => emit("update:filters", value),
})

// Empty/whitespace query never submits (trim-check before emitting).
const canSubmit = computed(() => props.draft.trim().length > 0)

function submitIfValid(): void {
  // No client queue (C6): useChat().send already no-ops a submit while
  // streaming (the contract-enforcing guard) -- this is the UX-level guard
  // so Enter/the button never even attempts it while the button reads
  // "Stop" (that path calls onSubmitControlClick's stop branch instead).
  if (props.disabled) return
  const query = props.draft.trim()
  if (!query) return
  emit("submit", { query, filters: props.filters })
  emit("update:draft", "")
}

function onKeydown(e: KeyboardEvent): void {
  // Enter submits, Shift+Enter newlines (default textarea behavior --
  // nothing to do for that case beyond not preventing default).
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault()
    submitIfValid()
  }
}

function onInput(e: Event): void {
  emit("update:draft", (e.target as HTMLTextAreaElement).value)
}

// The submit button SWAPS to "Stop" while streaming -- same control,
// re-labeled and re-wired, not a second one (Open Gap #5). Stop is the one
// control that stays clickable during a stream.
function onControlClick(): void {
  if (props.disabled) {
    emit("stop")
  } else {
    submitIfValid()
  }
}
</script>

<template>
  <form class="composer" @submit.prevent="submitIfValid">
    <!-- Typing stays allowed while streaming (Phase 5 note); only
         submission is gated, so no `disabled` attribute here -- the
         `--busy` class is a Phase 8 styling hook only. -->
    <textarea
      class="composer__textarea"
      :class="{ 'composer__textarea--busy': disabled }"
      :value="draft"
      placeholder="Ask Dostoevsky a question…"
      aria-label="Ask Dostoevsky a question"
      rows="3"
      @input="onInput"
      @keydown="onKeydown"
    ></textarea>

    <FilterControl v-if="!error" :tree="tree" v-model="filtersModel" />

    <button
      type="button"
      class="composer__submit"
      :class="{ 'composer__submit--stop': disabled }"
      :disabled="!disabled && !canSubmit"
      @click="onControlClick"
    >
      {{ disabled ? "Stop" : "Send" }}
    </button>
  </form>
</template>

<style scoped>
.composer {
  position: sticky;
  bottom: 0;
  z-index: var(--z-composer);
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-6);
  background: var(--panel);
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}

.composer__textarea {
  flex: 1 1 100%;
  min-width: 240px;
  resize: none;
  border: none;
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--ink);
  font-family: var(--font-chrome);
  font-size: var(--fs-body);
  line-height: 1.5;
  padding: var(--sp-3) var(--sp-4);
}

.composer__textarea::placeholder {
  color: var(--ink-muted);
}

/* Busy state (§III.7): typing stays allowed, styling hook only -- quiet
   tonal change, never amber (typing is not the actionable send affordance). */
.composer__textarea--busy {
  background: var(--panel-2);
}

/* Send -- one of the four permitted amber spots. */
.composer__submit {
  padding: var(--sp-3) var(--sp-5);
  border-radius: var(--radius-md);
  background: var(--accent);
  color: var(--on-accent);
  font-family: var(--font-chrome);
  font-weight: 600;
  font-size: var(--fs-label);
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease-out-exp),
    transform var(--dur-fast) var(--ease-out-exp);
}

.composer__submit:hover:not(:disabled) {
  background: var(--accent-hover);
}

/* Send press (§V.7, named transition). */
.composer__submit:active:not(:disabled) {
  background: var(--accent-active);
  transform: scale(0.98);
}

.composer__submit:disabled {
  background: var(--accent-muted);
  color: var(--on-accent);
  cursor: not-allowed;
}

.composer__submit--stop {
  background: var(--panel-2);
  color: var(--ink);
}

.composer__submit--stop:hover {
  background: var(--panel-2);
}
</style>
