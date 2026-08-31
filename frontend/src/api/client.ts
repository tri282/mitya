// Phase 4 (api-boundary): typed fetch wrappers for /chat, /health, /books.
// Same-origin relative paths only — never a hardcoded host (design §I.6),
// so the identical build works under the dev proxy and prod StaticFiles mount.

import type { Health, StructureTree } from "./types"

export async function getHealth(): Promise<Health> {
  const res = await fetch("/health")
  // 200 or 503 both return the same JSON shape — /health's degraded body
  // IS the payload, not an error to catch (HealthIndicator reads `status`
  // from the body regardless of HTTP status code).
  return res.json() as Promise<Health>
}

export async function getBooks(): Promise<StructureTree> {
  const res = await fetch("/books")
  if (!res.ok) throw new Error(`GET /books failed: ${res.status}`)
  return res.json() as Promise<StructureTree>
}

// --- auth (v2 P5 step 7) ----------------------------------------------------
// Same-origin relative paths, so the session cookie is sent by the browser
// with no `credentials` option needed and no token ever touching JS.

/** The tenant id of the current session, or null when there is none.
 *  401 is the ANSWER here, not an error -- it is how an unauthenticated
 *  visitor is identified, and throwing would make the login wall a failure
 *  state instead of the normal one. */
export async function getSession(): Promise<string | null> {
  try {
    const res = await fetch("/auth/session")
    if (res.status === 401) return null
    if (!res.ok) return null
    const body = (await res.json()) as { tenant: string }
    return body.tenant
  } catch {
    // Network failure: not authenticated as far as the UI is concerned. The
    // server is the only authority and it did not answer.
    return null
  }
}

/** Resolves to the tenant id, or throws with the status code as its message
 *  so the caller can distinguish a 429 from a rejection without learning
 *  anything the server did not say. */
export async function postLogin(username: string, password: string): Promise<string> {
  const res = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) throw new Error(String(res.status))
  const body = (await res.json()) as { tenant: string }
  return body.tenant
}

export async function postLogout(): Promise<void> {
  await fetch("/auth/logout", { method: "POST" })
}
