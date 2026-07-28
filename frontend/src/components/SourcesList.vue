<script setup lang="ts">
// Phase 6 (transcript-components): programmatic grounding, live AND cache
// path alike (design §III.6, DECIDE C10 -- no `cacheHit` prop, no cache-hit
// branching whatsoever). No-context detection is keyed off `sources.length
// === 0` (DECIDE C11), never `footer === ""`.

import { computed } from "vue"
import type { Source } from "../api/types"
import { formatChapter, formatPart, resolveBookNode } from "../bookLabels"
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
    // Shared lookup (Phase 7, C3) -- same helper QuestionLine's scope chip
    // uses. formatPart/formatChapter drop a null part/chapter instead of
    // interpolating "null" into the locator string (the prior inline
    // version assumed both were always present).
    const node = resolveBookNode(tree.value, source.book)
    if (node) {
      const locator = [formatPart(node, source.part), formatChapter(node, source.chapter)]
        .filter((segment): segment is string => segment !== null)
        .join(", ")
      return { source, book: source.book, locator }
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

<style scoped>
/* Source reveal (§V.7): fade + rise --sp-2 once, on the sources event
   landing -- this component only mounts once per turn (v-if in
   AnswerBlock), so a plain mount-time animation is the reveal. */
.sources-list {
  margin-top: var(--sp-5);
  animation: sources-reveal var(--dur-med) var(--ease-out-exp) both;
}

@keyframes sources-reveal {
  from {
    opacity: 0;
    transform: translateY(var(--sp-2));
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .sources-list {
    animation: none;
  }
}

.sources-list__no-context {
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  color: var(--ink-muted);
}

.sources-list__rows {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.sources-list__row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius-sm);
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  cursor: default;
  transition: background-color var(--dur-fast) var(--ease-out-exp);
}

.sources-list__row:hover,
.sources-list__row:focus-visible {
  background: var(--panel-2);
}

/* Source-link affordance (§V.2/§V.3) -- one of the four permitted amber
   spots. Book name reads as the citation itself. */
.sources-list__book {
  color: var(--accent);
}

.sources-list__locator {
  color: var(--ink-muted);
}
</style>
