<script setup lang="ts">
// Phase 6 (transcript-components): the user's question, quiet (design
// §III.4). Scope-chip labels come from the resolved BookNode's
// part_label/chapter_label -- never a hardcoded "Part" (DECIDE C3).

import { computed } from "vue"
import type { ChatFilters } from "../api/types"
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
  const node = tree.value?.[filters.book]
  // Tree not loaded yet, or an unresolved book name: render nothing rather
  // than fabricate a "Part"/"Chapter" label (C3 is non-negotiable). The
  // chip appears a beat later once useBooks()'s fetch resolves.
  if (!node) return null

  const segments = [filters.book]
  if (filters.part !== undefined) {
    segments.push(`${node.part_label} ${filters.part}`)
    if (filters.chapter !== undefined) {
      segments.push(`${node.chapter_label} ${filters.chapter}`)
    }
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
