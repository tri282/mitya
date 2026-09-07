// v2 Phase 5 (session-persistence): state/session.ts's tenant-scoped
// localStorage persistence -- the soft-cap/quota-retry and corrupt-load
// recovery from v1 (DECIDE C4), plus the new adopt/purge rules from design
// §3.4 (storage keys carry the tenant id, one watcher covers every
// transition, the reactivity-trap-safe splice).
//
// This is exactly the code the Node-26/jsdom shim (../testSetup.ts, wired
// via vite.config.ts's test.setupFiles) exists to unblock: without it every
// test below fails with "Cannot read properties of undefined (reading
// 'clear')" because Node's own `localStorage` accessor shadows jsdom's.
//
// session.ts registers a module-scope `watch(tenant, ...)` at import time,
// so every test drives the tenant transition through `auth.ts`'s `tenant`
// ref and awaits `nextTick()` -- Vue's watcher callback runs on the next
// microtask flush, not synchronously on assignment.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { nextTick } from "vue"
import type { Turn } from "./session"

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const sessionKey = (t: string) => `mitya.session.v1.${t}`
const transcriptKey = (t: string) => `mitya.transcript.v2.${t}`
const LEGACY_TRANSCRIPT_KEY = "mitya.transcript.v1"

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

describe("session adoption (design §3.4)", () => {
  it("mints and writes a UUID when no session id is stored for the tenant", async () => {
    const { tenant } = await import("./auth")
    const { sessionId } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    expect(sessionId.value).toMatch(UUID_RE)
    expect(localStorage.getItem(sessionKey("tenant-a"))).toBe(sessionId.value)
  })

  it("reuses a stored valid UUID verbatim", async () => {
    const stored = "11111111-1111-4111-8111-111111111111"
    localStorage.setItem(sessionKey("tenant-a"), stored)
    const { tenant } = await import("./auth")
    const { sessionId } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    expect(sessionId.value).toBe(stored)
  })

  it("replaces a garbage stored value with a fresh UUID", async () => {
    localStorage.setItem(sessionKey("tenant-a"), "not-a-uuid")
    const { tenant } = await import("./auth")
    const { sessionId } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    expect(sessionId.value).toMatch(UUID_RE)
    expect(sessionId.value).not.toBe("not-a-uuid")
  })

  it("reads the tenant-scoped transcript key and sweeps the legacy key", async () => {
    localStorage.setItem(transcriptKey("tenant-a"), JSON.stringify([makeTurn("1")]))
    localStorage.setItem(LEGACY_TRANSCRIPT_KEY, JSON.stringify([makeTurn("legacy")]))
    const { tenant } = await import("./auth")
    const { session } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    expect(session.turns.map((t) => t.id)).toEqual(["1"])
    expect(localStorage.getItem(LEGACY_TRANSCRIPT_KEY)).toBeNull()
  })

  it("is a no-op on a same-tenant transition (401-then-sign-back-in): transcript length unchanged", async () => {
    const { tenant } = await import("./auth")
    const { session, sessionId } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    session.turns.push(makeTurn("x"))
    const idBefore = sessionId.value

    // Session expiry: tenant -> null leaves in-memory state alone (rule 3).
    tenant.value = null
    await nextTick()
    expect(session.turns).toHaveLength(1)

    // Sign back in as the SAME tenant: no-op (rule 2), not a reload from disk.
    tenant.value = "tenant-a"
    await nextTick()
    expect(session.turns).toHaveLength(1)
    expect(sessionId.value).toBe(idBefore)
  })

  it("adopts a genuinely different tenant by splicing the array in place, never reassigning it", async () => {
    localStorage.setItem(transcriptKey("tenant-b"), JSON.stringify([makeTurn("b1")]))
    const { tenant } = await import("./auth")
    const { session } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    const turnsRef = session.turns // the reference App.vue/useChat would have captured
    session.turns.push(makeTurn("a1"))

    tenant.value = "tenant-b"
    await nextTick()
    expect(session.turns).toBe(turnsRef) // same array object -- splice, not reassignment
    expect(session.turns.map((t) => t.id)).toEqual(["b1"])
  })
})

describe("session persistence (quota-retry / corrupt-load, unchanged from v1)", () => {
  it("drops the oldest turns and retries on QuotaExceededError, without throwing to the caller", async () => {
    vi.useFakeTimers()
    const { tenant } = await import("./auth")
    const { session, scheduleSave } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
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
    const stored = JSON.parse(localStorage.getItem(transcriptKey("tenant-a"))!) as Turn[]
    // The 3 oldest turns were dropped; the 2 most recent survive.
    expect(stored.map((t) => t.id)).toEqual(["4", "5"])
  })

  it("terminates without looping forever when the backing array is already empty and every write throws", async () => {
    vi.useFakeTimers()
    const { tenant } = await import("./auth")
    const { session, scheduleSave } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
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
    localStorage.setItem(transcriptKey("tenant-a"), "{not valid json")
    const { tenant } = await import("./auth")
    const { session } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    expect(session.turns).toEqual([])
  })

  it("loads an empty in-memory transcript when the stored value parses but is not an array", async () => {
    localStorage.setItem(transcriptKey("tenant-a"), JSON.stringify({ not: "an array" }))
    const { tenant } = await import("./auth")
    const { session } = await import("./session")
    tenant.value = "tenant-a"
    await nextTick()
    expect(session.turns).toEqual([])
  })
})
