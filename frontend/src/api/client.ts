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
