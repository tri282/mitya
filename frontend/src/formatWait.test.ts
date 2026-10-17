import { describe, expect, it } from "vitest"
import { formatWait } from "./formatWait"

describe("formatWait", () => {
  it("uses the largest whole unit, rounded up", () => {
    expect(formatWait(0)).toBe("1 second")
    expect(formatWait(45)).toBe("45 seconds")
    expect(formatWait(60)).toBe("1 minute")
    expect(formatWait(61)).toBe("2 minutes")
    expect(formatWait(3 * 3600 - 10)).toBe("3 hours")
    expect(formatWait(86400)).toBe("1 day")
    expect(formatWait(2 * 86400 + 1)).toBe("3 days")
  })
})
