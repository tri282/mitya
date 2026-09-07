// v2 P5 step 7 tests: the login wall's state machine (state/auth.ts).
//
// What is worth testing here is small and specific, so this file is small and
// specific. Three properties, each of which is a real failure if it breaks:
//
//   1. an unauthenticated visitor is `tenant === null` with `checked === true`
//      -- 401 from /auth/session is the ANSWER, not an error, and treating it
//      as one would render an error state instead of the login wall;
//   2. a failed login produces ONE message regardless of cause (D7: the server
//      refuses to distinguish an unknown account from a wrong password, and
//      the UI must not reintroduce the distinction);
//   3. `onUnauthenticated()` drops to the wall -- this is the session-expiry
//      path, including mid-stream, and it is the one useChat calls on a 401.
//
// `fetch` is stubbed rather than msw'd: these are four one-line endpoints and
// a mock server would be more machinery than subject.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { nextTick } from "vue"

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

beforeEach(() => {
  vi.resetModules()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe("auth state", () => {
  it("treats a 401 from /auth/session as 'not signed in', not as an error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, { detail: "authentication required" })))
    const auth = await import("./auth")
    await auth.refresh()
    expect(auth.tenant.value).toBeNull()
    expect(auth.checked.value).toBe(true)
  })

  it("stores the tenant when the session is live", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { tenant: "tenant-a" })))
    const auth = await import("./auth")
    await auth.refresh()
    expect(auth.tenant.value).toBe("tenant-a")
  })

  it("reports a network failure as 'not signed in' rather than hanging on unknown", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline") }))
    const auth = await import("./auth")
    await auth.refresh()
    expect(auth.tenant.value).toBeNull()
    expect(auth.checked.value).toBe(true)
  })

  it("gives the same message for an unknown account and a wrong password", async () => {
    const auth = await import("./auth")
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, { detail: "invalid credentials" })))
    expect(await auth.login("nobody", "x")).toBe(false)
    const unknownAccountMessage = auth.loginError.value
    expect(await auth.login("tenant-a", "wrong")).toBe(false)
    expect(auth.loginError.value).toBe(unknownAccountMessage)
    expect(auth.tenant.value).toBeNull()
  })

  it("distinguishes only the rate limit, which is not an existence oracle", async () => {
    const auth = await import("./auth")
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(429, { detail: "too many attempts" })))
    expect(await auth.login("tenant-a", "x")).toBe(false)
    expect(auth.loginError.value).toMatch(/wait/i)
  })

  it("signs in and clears the previous error", async () => {
    const auth = await import("./auth")
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, {})))
    await auth.login("tenant-a", "wrong")
    expect(auth.loginError.value).not.toBeNull()
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { tenant: "tenant-a" })))
    expect(await auth.login("tenant-a", "right")).toBe(true)
    expect(auth.tenant.value).toBe("tenant-a")
    expect(auth.loginError.value).toBeNull()
  })

  it("drops to the login wall when a 401 arrives later (session expiry)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { tenant: "tenant-a" })))
    const auth = await import("./auth")
    await auth.refresh()
    expect(auth.tenant.value).toBe("tenant-a")
    auth.onUnauthenticated()
    expect(auth.tenant.value).toBeNull()
    expect(auth.checked.value).toBe(true)
  })

  it("clears the tenant on sign out", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { tenant: "tenant-a" })))
    const auth = await import("./auth")
    await auth.refresh()
    await auth.logout()
    expect(auth.tenant.value).toBeNull()
  })

  // v2 Phase 5 (session-persistence, design §3.4/§6): logout() must purge
  // both storage keys for the signed-out tenant BEFORE clearing `tenant`,
  // and a storage failure must never block sign-out.
  it("purges both storage keys and still clears the tenant when localStorage.removeItem throws", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { tenant: "tenant-a" })))
    const auth = await import("./auth")
    // In the real app state/session.ts is imported (and its module-scope
    // watch(tenant, ...) registered) before any tenant transition, via
    // useChat.ts at App.vue setup. Import it here too so the watcher can
    // adopt "tenant-a" below -- otherwise `currentTenant` stays null and
    // purge() no-ops before ever touching storage.
    await import("./session")
    await auth.refresh()
    await nextTick() // let the watch callback (adopt) run

    const removeItemSpy = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("storage disabled")
    })

    await expect(auth.logout()).resolves.toBeUndefined()
    expect(auth.tenant.value).toBeNull()
    // Both tenant-scoped keys were attempted, in spite of the throw.
    expect(removeItemSpy).toHaveBeenCalledWith("mitya.session.v1.tenant-a")
    expect(removeItemSpy).toHaveBeenCalledWith("mitya.transcript.v2.tenant-a")
  })
})
