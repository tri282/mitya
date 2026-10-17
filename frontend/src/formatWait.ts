// Human wait time for a spend refusal's retry_after_s (v3 P0 OI8 client contract).

/** "45 seconds", "3 hours", "2 days": the largest whole unit, rounded up. */
export function formatWait(seconds: number): string {
  const units: [number, string][] = [
    [86400, "day"],
    [3600, "hour"],
    [60, "minute"],
  ]
  for (const [size, name] of units) {
    if (seconds >= size) {
      const n = Math.ceil(seconds / size)
      return `${n} ${name}${n === 1 ? "" : "s"}`
    }
  }
  const s = Math.max(1, Math.ceil(seconds))
  return `${s} second${s === 1 ? "" : "s"}`
}
