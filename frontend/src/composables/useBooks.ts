// Phase 5 (state-composables): loads GET /books once for FilterControl's tree.
// Coercion of the tree into ChatFilters (part/chapter string -> number, per
// DECIDE C3) happens at the Phase 7 FilterControl call site, not here.
//
// Phase 6 note: moved `tree`/`error` to module scope (mirroring useHealth.ts's
// singleton) so QuestionLine's scope-chip label lookup (DECIDE C3) and Phase
// 7's FilterControl share one fetch/tree instead of drifting independent
// copies -- the phase-6 contract calls for the SAME sharing pattern used for
// useHealth, not a new one.

import { onMounted, ref } from "vue"
import { getBooks } from "../api/client"
import type { StructureTree } from "../api/types"

const tree = ref<StructureTree | null>(null)
// Drives FilterControl's "books fetch failed -> control hidden, unscoped
// queries still work" state (design §III.8).
const error = ref(false)

async function load(): Promise<void> {
  try {
    tree.value = await getBooks()
  } catch {
    error.value = true
  }
}

// Same fan-out trap as useHealth: the tree is shared, but onMounted() runs per
// *calling component*, so /books refetched every time a QuestionLine or
// SourcesList mounted for a new turn. Load exactly once per page load.
let loadStarted = false

export function useBooks() {
  onMounted(() => {
    if (loadStarted) return
    loadStarted = true
    void load()
  })
  return { tree, error }
}
