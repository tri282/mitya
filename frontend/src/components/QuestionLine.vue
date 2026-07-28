<script setup lang="ts">
// Phase 6 (transcript-components): the user's question, quiet (design
// §III.4). Scope-chip labels come from the resolved BookNode's
// part_label/chapter_label -- never a hardcoded "Part" (DECIDE C3).

import { computed } from "vue"
import type { ChatFilters } from "../api/types"
import { formatChapter, formatPart, resolveBookNode } from "../bookLabels"
import { useBooks } from "../composables/useBooks"

const props = defineProps<{
  question: string
  filters?: ChatFilters
}>()

// Shared useBooks() singleton (see useBooks.ts's Phase 6 note) -- same
// sharing pattern as useHealth.ts, not a new one.
const { tree } = useBooks()

const chipLabel = computed(() => {
  const filters = props.filters
  if (!filters?.book) return null
  // Shared lookup (Phase 7, C3): tree not loaded yet, or an unresolved book
  // name, resolves to undefined -- render nothing rather than fabricate a
  // "Part"/"Chapter" label. The chip appears a beat later once useBooks()'s
  // fetch resolves.
  const node = resolveBookNode(tree.value, filters.book)
  if (!node) return null

  const segments = [filters.book]
  const partLabel = formatPart(node, filters.part)
  if (partLabel) {
    segments.push(partLabel)
    const chapterLabel = formatChapter(node, filters.chapter)
    if (chapterLabel) segments.push(chapterLabel)
  }
  return segments.join(" · ")
})
</script>

<template>
  <div class="question-line">
    <p class="question-line__text">{{ question }}</p>
    <span v-if="chipLabel" class="question-line__scope-chip">{{ chipLabel }}</span>
  </div>
</template>

<style scoped>
.question-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}

.question-line__text {
  font-family: var(--font-chrome);
  font-size: var(--fs-body);
  color: var(--ink-muted);
}

.question-line__scope-chip {
  font-family: var(--font-chrome);
  font-size: var(--fs-meta);
  color: var(--ink-muted);
  background: var(--panel);
  border-radius: var(--radius-sm);
  padding: var(--sp-1) var(--sp-2);
}
</style>
