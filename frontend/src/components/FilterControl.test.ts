// Phase 10 (tests): FilterControl's book -> part -> chapter cascade and the
// numeric part/chapter coercion at emit time (DECIDE C3) -- this is the
// component-under-test the design explicitly carves out of the "no
// component tests" rule, because the cascade/coercion logic has no visual
// signal when it silently breaks. No snapshot/visual assertions.

import { describe, expect, it } from "vitest"
import { mount } from "@vue/test-utils"
import FilterControl from "./FilterControl.vue"
import { formatChapter } from "../bookLabels"
import type { ChatFilters, StructureTree } from "../api/types"

// Same trimmed fixture shape as useBooks.test.ts (duplicated intentionally --
// phase-10-tests.md scopes exactly five test files, so no shared fixture
// module was added).
const fixtureTree: StructureTree = {
  "Crime and Punishment": {
    file: "crime_and_punishment.txt",
    scheme: "part_chapter",
    part_label: "Part",
    chapter_label: "Chapter",
    notes: null,
    parts: {
      "1": { chapters: [1, 2, 3], unit_count: 3 },
      "2": { chapters: [1, 2], unit_count: 2 },
    },
    total_units: 5,
  },
  "The Brothers Karamazov": {
    file: "the_brothers_karamazov.txt",
    scheme: "book_chapter",
    part_label: "Book",
    chapter_label: "Chapter",
    notes: null,
    parts: {
      "1": { chapters: [1, 2], unit_count: 2 },
    },
    total_units: 2,
  },
  // Real shape from data/rag/structure.json: chapters are unlabeled bare
  // roman numerals, so the generator emits a NULL chapter_label. Interpolating
  // it raw rendered "null 5" in the source locator.
  "The Idiot": {
    file: "the_idiot.txt",
    scheme: "part_chapter",
    part_label: "Part",
    chapter_label: null,
    notes: "chapters are unlabeled bare roman numerals",
    parts: {
      "1": { chapters: [1, 2], unit_count: 2 },
    },
    total_units: 2,
  },
}

function mountControl(modelValue: ChatFilters = {}) {
  return mount(FilterControl, {
    props: { tree: fixtureTree, modelValue },
  })
}

/** Last emitted `update:modelValue` payload, or undefined if none yet. */
function lastEmittedFilters(wrapper: ReturnType<typeof mountControl>): ChatFilters | undefined {
  const emitted = wrapper.emitted<[ChatFilters]>("update:modelValue")
  return emitted?.at(-1)?.[0]
}

describe("FilterControl", () => {
  it("selecting a book populates the part select's options from that book's parts", async () => {
    const wrapper = mountControl()
    const selects = wrapper.findAll("select")
    await selects[0].setValue("Crime and Punishment")

    const partOptions = wrapper.findAll("select")[1].findAll("option").map((o) => o.text())
    expect(partOptions).toEqual(["All", "1", "2"])
  })

  it("uses the book's own part_label -- Karamazov reads 'Book', not 'Part'", async () => {
    const wrapper = mountControl()
    await wrapper.findAll("select")[0].setValue("The Brothers Karamazov")

    expect(wrapper.text()).toContain("Book")
    expect(wrapper.text()).not.toContain("Part")
  })

  it("selecting a part populates the chapter select's options", async () => {
    const wrapper = mountControl()
    await wrapper.findAll("select")[0].setValue("Crime and Punishment")
    await wrapper.findAll("select")[1].setValue("1")

    const chapterOptions = wrapper.findAll("select")[2].findAll("option").map((o) => o.text())
    expect(chapterOptions).toEqual(["All", "1", "2", "3"])
  })

  it("changing the book resets the part/chapter selection -- no stale cross-book state", async () => {
    const wrapper = mountControl()
    await wrapper.findAll("select")[0].setValue("Crime and Punishment")
    await wrapper.findAll("select")[1].setValue("1")
    await wrapper.findAll("select")[2].setValue("2")

    expect(lastEmittedFilters(wrapper)).toEqual({
      book: "Crime and Punishment",
      part: 1,
      chapter: 2,
    })

    // Switching books: only Book + Part selects should remain (chapter
    // select disappears until a part is chosen again), and the emitted
    // filters must not carry the previous book's part/chapter forward.
    await wrapper.findAll("select")[0].setValue("The Brothers Karamazov")

    expect(wrapper.findAll("select")).toHaveLength(2)
    expect(lastEmittedFilters(wrapper)).toEqual({ book: "The Brothers Karamazov" })
  })

  it("emits ChatFilters with part/chapter coerced to number, not string", async () => {
    const wrapper = mountControl()
    await wrapper.findAll("select")[0].setValue("Crime and Punishment")
    await wrapper.findAll("select")[1].setValue("2")
    await wrapper.findAll("select")[2].setValue("1")

    const filters = lastEmittedFilters(wrapper)
    expect(filters).toEqual({ book: "Crime and Punishment", part: 2, chapter: 1 })
    expect(typeof filters?.part).toBe("number")
    expect(typeof filters?.chapter).toBe("number")
  })

  it("falls back to 'Chapter' when the book has a null chapter_label", async () => {
    const wrapper = mountControl()
    await wrapper.findAll("select")[0].setValue("The Idiot")
    await wrapper.findAll("select")[1].setValue("1")

    expect(wrapper.findAll("select")).toHaveLength(3)
    expect(wrapper.text()).toContain("Chapter")
    expect(wrapper.text()).not.toContain("null")

    // Same fallback on the read side -- this is where "null 5" surfaced, in
    // SourcesList's and QuestionLine's locator.
    expect(formatChapter(fixtureTree["The Idiot"], 5)).toBe("Chapter 5")
  })

  it("clearing the book scope resets all downstream selections", async () => {
    const wrapper = mountControl()
    await wrapper.findAll("select")[0].setValue("Crime and Punishment")
    await wrapper.findAll("select")[1].setValue("1")

    await wrapper.find("button.filter-control__clear").trigger("click")

    expect(wrapper.findAll("select")).toHaveLength(1)
    expect(lastEmittedFilters(wrapper)).toEqual({})
  })
})
