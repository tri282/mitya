<script setup lang="ts">
// Phase 6 (transcript-components): the reading -- streaming prose, then
// SourcesList (design §III.5). Rendering safety (DECIDE C5, non-negotiable):
// `turn.answer` is split into paragraphs and rendered through Vue text
// interpolation ({{ }}), which always produces text nodes -- never `v-html`,
// never a markdown parse.

import { computed } from "vue"
import type { Turn } from "../state/session"
import DebugRow from "./DebugRow.vue"
import SourcesList from "./SourcesList.vue"

const props = defineProps<{
  turn: Turn
}>()

// Boundary deviation (flagged in phase-6.md log): design §III.5 lists
// AnswerBlock's boundary as "no events", but §VI requires a *working*
// Try-again control on 503/504 that re-submits the same turn. A working
// retry must go through the single shared useChat() instance (design
// §II.4 -- isStreaming is one source of truth so header/composer/transcript
// agree), which AnswerBlock does not own, so it emits upward instead of
// calling send() itself.
const emit = defineEmits<{
  retry: []
}>()

/** Split on the model's paragraph convention (blank line) and drop any
 * empty segments from leading/trailing/duplicate blank lines. */
function toParagraphs(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter((p) => p.length > 0)
}

const paragraphs = computed(() => toParagraphs(props.turn.answer))

// §VI empty-answer copy applies only once the turn has actually closed with
// nothing to show -- not mid-stream, where an empty answer just means
// "no tokens yet" (judgment call, see phase-6.md log).
const isEmptyComplete = computed(
  () => props.turn.state === "complete" && paragraphs.value.length === 0,
)
</script>

<template>
  <div class="answer-block">
    <!-- streaming / complete / stream-error / interrupted: prose renders,
         paragraph by paragraph, as plain text nodes only (C5). -->
    <template
      v-if="
        turn.state === 'streaming' ||
        turn.state === 'complete' ||
        turn.state === 'stream-error' ||
        turn.state === 'interrupted'
      "
    >
      <p v-if="isEmptyComplete" class="answer-block__empty">
        Mitya considered your question but had nothing to say. Perhaps ask it differently.
      </p>
      <template v-else>
        <p v-for="(paragraph, i) in paragraphs" :key="i" class="answer-block__paragraph">
          {{ paragraph }}
        </p>
      </template>

      <!-- Caret placeholder for Phase 8's Reading Lamp animation; purely
           structural here, visual treatment lands later. -->
      <span v-if="turn.state === 'streaming'" class="answer-block__caret" aria-hidden="true"></span>

      <p v-if="turn.state === 'stream-error'" class="answer-block__notice">
        …the thread was lost mid-sentence. (connection interrupted)
      </p>

      <!-- turn.state === "interrupted": no §VI copy exists for this state
           (flagged as a copy gap in phase-6.md) -- keep the partial prose
           as-is, no error block invented. -->

      <template v-if="turn.state === 'complete'">
        <p v-if="turn.meta?.filter_relaxed" class="answer-block__note">
          No passage matched that exact scope, so Mitya read more widely.
        </p>
        <SourcesList :sources="turn.sources" :footer="turn.footer" />
      </template>

      <!-- Rendered for every reading state (streaming/complete/stream-error/
           interrupted) so it shows "diagnostics unavailable" from the first
           token, then populates once `meta` arrives last in the stream
           (design §IV, C1, this phase's Step 5). Not rendered for the pure
           error-4xx/5xx states below, which never produce a `meta` either. -->
      <DebugRow :meta="turn.meta" />
    </template>

    <template v-else-if="turn.state === 'error-400'">
      <p class="answer-block__error">
        That book or chapter isn't one Mitya knows — try another scope.
      </p>
    </template>

    <template v-else-if="turn.state === 'error-503'">
      <p class="answer-block__error">
        The lamp is out — Mitya's voice can't be reached just now.
      </p>
      <button type="button" class="answer-block__retry" @click="emit('retry')">Try again</button>
    </template>

    <template v-else-if="turn.state === 'error-504'">
      <p class="answer-block__error">
        Mitya paused too long over your question and the moment passed. Ask again?
      </p>
      <button type="button" class="answer-block__retry" @click="emit('retry')">Try again</button>
    </template>
  </div>
</template>

<style scoped>
.answer-block {
  max-width: 66ch;
}

.answer-block__paragraph,
.answer-block__empty {
  font-family: var(--font-reading);
  font-size: var(--fs-reading);
  line-height: 1.7;
  color: var(--ink);
}

.answer-block__paragraph + .answer-block__paragraph {
  margin-top: var(--sp-4);
}

/* The Reading Lamp (§V.8) -- the ONE signature element. Boldness is spent
   here and nowhere else in the system. */
.answer-block__caret {
  display: inline-block;
  width: 3px;
  height: 1.1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--accent);
  border-radius: 1px;
  box-shadow: var(--shadow-ambient);
  animation: reading-lamp 1.6s ease-in-out infinite;
}

@keyframes reading-lamp {
  0%,
  100% {
    opacity: 0.5;
  }
  50% {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .answer-block__caret {
    animation: none;
    opacity: 1;
  }
}

.answer-block__notice,
.answer-block__note {
  margin-top: var(--sp-3);
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  color: var(--ink-muted);
}

.answer-block__empty {
  color: var(--ink-muted);
}

.answer-block__error {
  font-family: var(--font-chrome);
  font-size: var(--fs-body);
  color: var(--ink);
}

.answer-block__retry {
  margin-top: var(--sp-3);
  padding: var(--sp-2) var(--sp-4);
  border-radius: var(--radius-sm);
  background: var(--panel);
  color: var(--ink);
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  font-weight: 500;
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease-out-exp);
}

.answer-block__retry:hover {
  background: var(--panel-2);
}
</style>
