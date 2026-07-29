// Phase 10 (tests): useBooks.ts's actual contract -- a module-level
// singleton `/books` fetch, guarded against the once-per-component-mount
// fan-out fixed during P6/P7 (STATUS.md), exposing `tree`/`error` refs.
//
// Judgment call (flagged, not silently decided): phase-10-tests.md's Step 4
// prose describes asserting "ChatFilters.part/.chapter come out as number"
// and resolving "the label for Karamazov" as if both live in useBooks.ts.
// They don't: useBooks.ts does no coercion at all (its own header comment
// says so) -- part/chapter string->number coercion lives in
// FilterControl.vue's private emitFilters() (covered by Step 5's
// FilterControl.test.ts, which explicitly re-states the same numeric-
// coercion assertion), and per-book label resolution lives in
// ../bookLabels.ts's resolveBookNode/part_label, a module not named among
// the five test files this phase permits. Rather than inventing coercion
// code that isn't there or adding a sixth test file, this file tests
// useBooks.ts's real behavior (fetch-once guard, tree/error population) and
// additionally exercises resolveBookNode directly against the SAME tree
// useBooks.ts fetched, which is the substantive claim Step 4 cares about
// (Karamazov resolves to "Book", not the hardcoded "Part") without
// duplicating Step 5's numeric-coercion assertion or adding a new file.

import { beforeEach, describe, expect, it, vi } from "vitest"
import { defineComponent, h } from "vue"
import { mount, flushPromises } from "@vue/test-utils"
import { resolveBookNode } from "../bookLabels"
import type { StructureTree } from "../api/types"

// Trimmed subset of data/rag/structure.json -- one "Part"-scheme book, one
// "Book"-scheme book (Karamazov), verified against the real file.
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
}

const { getBooksMock } = vi.hoisted(() => ({ getBooksMock: vi.fn() }))
vi.mock("../api/client", () => ({ getBooks: getBooksMock }))

beforeEach(() => {
  vi.resetModules()
  getBooksMock.mockReset()
})

/** Mount N host components that each call useBooks() on setup, so
 * onMounted actually fires (useBooks relies on it internally). */
async function mountUseBooksTimes(times: number) {
  const { useBooks } = await import("./useBooks")
  const results: ReturnType<typeof useBooks>[] = []
  for (let i = 0; i < times; i++) {
    const Host = defineComponent({
      setup() {
        results.push(useBooks())
        return () => h("div")
      },
    })
    mount(Host)
  }
  await flushPromises()
  return results
}

describe("useBooks", () => {
  it("fetches /books exactly once even when mounted from multiple components (fan-out guard)", async () => {
    getBooksMock.mockResolvedValue(fixtureTree)
    const [first, second] = await mountUseBooksTimes(2)

    expect(getBooksMock).toHaveBeenCalledOnce()
    expect(first.tree.value).toEqual(fixtureTree)
    expect(second.tree.value).toEqual(fixtureTree)
  })

  it("sets error and leaves tree null when the fetch rejects", async () => {
    getBooksMock.mockRejectedValue(new Error("network down"))
    const [result] = await mountUseBooksTimes(1)

    expect(result.error.value).toBe(true)
    expect(result.tree.value).toBeNull()
  })

  it("resolves the correct per-book label off the fetched tree -- Karamazov reads 'Book', not hardcoded 'Part'", async () => {
    getBooksMock.mockResolvedValue(fixtureTree)
    const [result] = await mountUseBooksTimes(1)

    expect(resolveBookNode(result.tree.value, "The Brothers Karamazov")?.part_label).toBe("Book")
    expect(resolveBookNode(result.tree.value, "Crime and Punishment")?.part_label).toBe("Part")
  })
})
