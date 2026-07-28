<script setup lang="ts">
// Phase 6 (transcript-components): teaches the interface before the first
// turn (design §III.10). No props. Emits `example-select` so the parent can
// populate the composer -- Composer itself is not wired until Phase 7, so
// App.vue just holds the picked query/book for now (see App.vue's note).

interface ExamplePrompt {
  query: string
  book: string
}

// Copy + book mapping resolved by the user 2026-07-27 (plan.md Open Gap #4,
// design §III.10 step 6): one prompt per book, each chip also sets the
// matching FilterControl scope. Verbatim strings -- do not edit.
const examples: ExamplePrompt[] = [
  { query: "What does Ivan mean by returning his ticket?", book: "The Brothers Karamazov" },
  { query: "Why does Raskolnikov confess?", book: "Crime and Punishment" },
  { query: "Why does Myshkin's goodness fail?", book: "The Idiot" },
]

const emit = defineEmits<{
  "example-select": [payload: ExamplePrompt]
}>()
</script>

<template>
  <div class="empty-state">
    <p class="empty-state__lead">
      Ask Dostoevsky a question. He answers in his own voice, from his own books.
    </p>
    <p class="empty-state__sub">
      Each question stands alone; Mitya keeps no memory of the conversation. Scope a
      question to a book or chapter with the filter below.
    </p>
    <ul class="empty-state__examples">
      <li v-for="example in examples" :key="example.query">
        <button type="button" class="empty-state__chip" @click="emit('example-select', example)">
          {{ example.query }}
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.empty-state {
  max-width: 66ch;
  padding-top: var(--sp-6);
}

.empty-state__lead {
  font-family: var(--font-reading);
  font-size: var(--fs-display);
  font-weight: 500;
  line-height: 1.4;
  color: var(--ink);
}

.empty-state__sub {
  margin-top: var(--sp-4);
  font-family: var(--font-chrome);
  font-size: var(--fs-body);
  line-height: 1.5;
  color: var(--ink-muted);
}

.empty-state__examples {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-top: var(--sp-6);
}

.empty-state__chip {
  display: block;
  width: 100%;
  text-align: left;
  padding: var(--sp-3) var(--sp-4);
  border-radius: var(--radius-md);
  background: var(--panel);
  color: var(--ink);
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease-out-exp);
}

.empty-state__chip:hover {
  background: var(--panel-2);
}
</style>
