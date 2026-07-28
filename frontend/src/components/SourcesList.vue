<script setup lang="ts">
// Phase 6 (transcript-components): programmatic grounding, live AND cache
// path alike (design §III.6, DECIDE C10 -- no `cacheHit` prop, no cache-hit
// branching whatsoever). No-context detection is keyed off `sources.length
// === 0` (DECIDE C11), never `footer === ""`.

import { computed } from "vue"
import type { Source } from "../api/types"
import { useBooks } from "../composables/useBooks"

const props = defineProps<{
  sources: Source[]
  footer: string
}>()

// Shared useBooks() singleton, same lookup as QuestionLine's scope chip:
// part_label/chapter_label come from the resolved BookNode, never hardcoded
// (DECIDE C3).
const { tree } = useBooks()

// Rows are built from `sources` (authoritative, per-field) rather than by
// parsing `footer`, so Phase 8 can style book and locator separately. The
// server-rendered footer (src/rag/generate.py:build_sources_footer, same
// order, same labels) is the fallback for the window before useBooks()
// resolves -- it is already correctly labeled, so no "Part" is fabricated.
const rows = computed(() => {
  const footerLines = props.footer.split("\n").slice(1) // drop the "Sources:" header
  return props.sources.map((source, i) => {
    const node = tree.value?.[source.book]
    if (node) {
      return {
        source,
        book: source.book,
        locator: `${node.part_label} ${source.part}, ${node.chapter_label} ${source.chapter}`,
      }
    }
    const line = footerLines[i]?.replace(/^\*\s*/, "")
    return { source, book: line ?? source.book, locator: "" }
  })
})
</script>

<template>
  <div class="sources-list">
    <p v-if="sources.length === 0" class="sources-list__no-context">
      Mitya answered from memory alone — no passage was close enough to cite.
    </p>
    <ul v-else class="sources-list__rows">
      <li
        v-for="(row, i) in rows"
        :key="`${row.source.book}-${row.source.part}-${row.source.chapter}-${i}`"
        class="sources-list__row"
        tabindex="0"
      >
        <span class="sources-list__book">{{ row.book }}</span>
        <span v-if="row.locator" class="sources-list__locator">{{ row.locator }}</span>
      </li>
    </ul>
  </div>
</template>
