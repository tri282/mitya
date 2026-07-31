<script setup lang="ts">
// Phase 7 (composer-filter-debug): scope a query by book -> part -> chapter
// (design §III.8, DECIDE C3). Cascading select off the /books tree; labels
// are per-book via the shared bookLabels helper -- never a hardcoded
// "Part" (Karamazov uses "Book"). Coercion of part/chapter (string tree
// keys -> number) happens here, at emit time, into the ChatFilters v-model.

import { computed, ref, watch } from "vue"
import type { ChatFilters, StructureTree } from "../api/types"
import { DEFAULT_CHAPTER_LABEL, resolveBookNode } from "../bookLabels"

const props = defineProps<{
  tree: StructureTree | null
}>()

const modelValue = defineModel<ChatFilters>({ required: true })

// Local string-keyed selection state mirrors the tree's string part keys;
// coerced to `number` only when emitting into the ChatFilters v-model (C3).
// `book` stays the display-name string throughout.
const selectedBook = ref<string>(modelValue.value.book ?? "")
const selectedPart = ref<string>(
  modelValue.value.part !== undefined ? String(modelValue.value.part) : "",
)
const selectedChapter = ref<string>(
  modelValue.value.chapter !== undefined ? String(modelValue.value.chapter) : "",
)

const bookNames = computed(() => (props.tree ? Object.keys(props.tree) : []))
const selectedNode = computed(() => resolveBookNode(props.tree, selectedBook.value || undefined))
const partKeys = computed(() => (selectedNode.value ? Object.keys(selectedNode.value.parts) : []))
const chapters = computed(() => {
  const node = selectedNode.value
  if (!node || !selectedPart.value) return []
  return node.parts[selectedPart.value]?.chapters ?? []
})

function emitFilters(): void {
  const filters: ChatFilters = {}
  if (selectedBook.value) {
    filters.book = selectedBook.value
    if (selectedPart.value) {
      filters.part = Number(selectedPart.value)
      if (selectedChapter.value) {
        filters.chapter = Number(selectedChapter.value)
      }
    }
  }
  modelValue.value = filters
}

function onBookChange(): void {
  // Choosing a new book invalidates any part/chapter selected under the
  // previous book (the cascade only makes sense one book at a time).
  selectedPart.value = ""
  selectedChapter.value = ""
  emitFilters()
}

function onPartChange(): void {
  selectedChapter.value = ""
  emitFilters()
}

function clearSelection(): void {
  selectedBook.value = ""
  selectedPart.value = ""
  selectedChapter.value = ""
  emitFilters()
}

// Keep the local selects in sync when the v-model is set from outside this
// control's own selects -- e.g. EmptyState's example chips (design §III.10)
// setting `{ book }` via App.vue's composerFilters.
watch(
  modelValue,
  (value) => {
    selectedBook.value = value.book ?? ""
    selectedPart.value = value.part !== undefined ? String(value.part) : ""
    selectedChapter.value = value.chapter !== undefined ? String(value.chapter) : ""
  },
  { deep: true },
)
</script>

<template>
  <!-- Loading: tree not yet fetched -- skeleton labels, no spinner
       (design §III.8). -->
  <div v-if="!tree" class="filter-control filter-control--loading" aria-hidden="true">
    <span class="filter-control__skeleton">Book</span>
    <span class="filter-control__skeleton">Part</span>
  </div>

  <!-- Error state (useBooks().error) is handled one level up, by Composer,
       which does not render this component at all -- "control hidden
       entirely" per design §III.8. -->
  <div v-else class="filter-control">
    <label class="filter-control__field">
      <span class="filter-control__label">Book</span>
      <select v-model="selectedBook" class="filter-control__select" @change="onBookChange">
        <option value="">All of Dostoevsky</option>
        <option v-for="name in bookNames" :key="name" :value="name">{{ name }}</option>
      </select>
    </label>

    <label v-if="selectedNode" class="filter-control__field">
      <span class="filter-control__label">{{ selectedNode.part_label }}</span>
      <select v-model="selectedPart" class="filter-control__select" @change="onPartChange">
        <option value="">All</option>
        <option v-for="key in partKeys" :key="key" :value="key">{{ key }}</option>
      </select>
    </label>

    <label v-if="selectedNode && selectedPart" class="filter-control__field">
      <span class="filter-control__label">{{
        selectedNode.chapter_label ?? DEFAULT_CHAPTER_LABEL
      }}</span>
      <select v-model="selectedChapter" class="filter-control__select" @change="emitFilters">
        <option value="">All</option>
        <option v-for="chapter in chapters" :key="chapter" :value="String(chapter)">
          {{ chapter }}
        </option>
      </select>
    </label>

    <!-- Active-selection state: a clearable quiet chip once a book is
         scoped (design §III.8). -->
    <button v-if="selectedBook" type="button" class="filter-control__clear" @click="clearSelection">
      Clear scope
    </button>
  </div>
</template>

<style scoped>
.filter-control,
.filter-control--loading {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--sp-3);
}

.filter-control__field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.filter-control__label {
  font-family: var(--font-chrome);
  font-size: var(--fs-meta);
  font-weight: 500;
  color: var(--ink-muted);
}

.filter-control__select {
  border: none;
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  padding: var(--sp-2) var(--sp-3);
}

.filter-control__clear {
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--ink-muted);
  font-family: var(--font-chrome);
  font-size: var(--fs-meta);
  padding: var(--sp-2) var(--sp-3);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-out-exp);
}

.filter-control__clear:hover {
  color: var(--ink);
}

.filter-control__skeleton {
  display: inline-block;
  width: 72px;
  height: 1.4em;
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  color: transparent;
}
</style>
