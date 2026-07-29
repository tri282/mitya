// Phase 10 (tests): state/session.ts's transcript persistence -- the
// localStorage soft-cap/quota-retry and corrupt-load recovery (DECIDE C4).
// This is exactly the code the Node-26/jsdom shim (../testSetup.ts, wired
// via vite.config.ts's test.setupFiles) exists to unblock: without it every
// test below fails with "Cannot read properties of undefined (reading
// 'clear')" because Node's own `localStorage` accessor shadows jsdom's.
//
// session.ts runs `loadTurns()` once at module-import time, so tests that
// need a specific pre-existing localStorage value must seed it BEFORE a
// fresh dynamic import (vi.resetModules() + `await import(...)`), not via
// the module's already-cached singleton.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { Turn } from "./session"

const STORAGE_KEY = "mitya.transcript.v1"

function makeTurn(id: string): Turn {
  return {
    id,
    question: `question ${id}`,
    answer: `answer ${id}`,
    sources: [],
    footer: "",
    state: "complete",
    createdAt: Date.now(),
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.resetModules()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe("session persistence", () => {
  it("drops the oldest turns and retries on QuotaExceededError, without throwing to the caller", async () => {
    vi.useFakeTimers()
    const { session, scheduleSave } = await import("./session")
    for (const id of ["1", "2", "3", "4", "5"]) session.turns.push(makeTurn(id))

    const realSetItem = Storage.prototype.setItem
    let throwsRemaining = 3
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      if (throwsRemaining > 0) {
        throwsRemaining--
        throw new DOMException("quota exceeded", "QuotaExceededError")
      }
      return realSetItem.call(this, key, value)
    })

    expect(() => {
      scheduleSave()
      vi.advanceTimersByTime(200)
    }).not.toThrow()

    // 3 throws + 1 successful write.
    expect(setItemSpy).toHaveBeenCalledTimes(4)
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!) as Turn[]
    // The 3 oldest turns were dropped; the 2 most recent survive.
    expect(stored.map((t) => t.id)).toEqual(["4", "5"])
  })

  it("terminates without looping forever when the backing array is already empty and every write throws", async () => {
    vi.useFakeTimers()
    const { session, scheduleSave } = await import("./session")
    expect(session.turns).toEqual([])

    const setItemSpy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("quota exceeded", "QuotaExceededError")
      })

    expect(() => {
      scheduleSave()
      vi.advanceTimersByTime(200)
    }).not.toThrow()

    // Empty array short-circuits after the first failed attempt -- no retry loop.
    expect(setItemSpy).toHaveBeenCalledTimes(1)
  })

  it("loads an empty in-memory transcript, not a thrown exception, when the stored value is corrupt JSON", async () => {
    localStorage.setItem(STORAGE_KEY, "{not valid json")
    const { session } = await import("./session")
    expect(session.turns).toEqual([])
  })

  it("loads an empty in-memory transcript when the stored value parses but is not an array", async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ not: "an array" }))
    const { session } = await import("./session")
    expect(session.turns).toEqual([])
  })
})
