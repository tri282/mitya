// Phase 10 (tests): SSE frame parsing/ordering/abort for chatStream.ts's
// hand-rolled fetch-body parser. This is the piece that breaks silently --
// a mis-split record or a swallowed abort produces no visual signal, only a
// wrong/stuck transcript. Scope per phase-10-tests.md: parsing, ordering,
// malformed-frame drop, chunk-boundary split, abort -- no component/markup.

import { afterEach, describe, expect, it, vi } from "vitest"
import { streamChat } from "./chatStream"
import type { ChatStreamEvent } from "./types"

/** Build a ReadableStream<Uint8Array> that yields exactly the given text
 * chunks (already split by the caller at whatever byte boundary a test
 * wants to exercise), then closes. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  let i = 0
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]))
        i++
      } else {
        controller.close()
      }
    },
  })
}

function mockFetchOnce(body: ReadableStream<Uint8Array>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValueOnce({
      ok: true,
      body,
      json: async () => ({}),
    } as unknown as Response),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("streamChat", () => {
  it("yields token* -> sources -> meta -> done in order on the live path", async () => {
    const text =
      `event: token\ndata: {"text":"Once "}\n\n` +
      `event: token\ndata: {"text":"upon a time"}\n\n` +
      `event: sources\ndata: {"sources":[{"book":"Crime and Punishment","part":1,"chapter":1}],"footer":"Crime and Punishment, Part 1, Chapter 1"}\n\n` +
      `event: meta\ndata: {"route_name":"HYBRID","chunk_ids":[1,2],"ttft_ms":120,"total_ms":900,"filter_relaxed":false,"no_context":false,"cache_hit":false}\n\n` +
      `event: done\ndata: {"complete":true,"finish_reason":"stop","refusal":null}\n\n`
    mockFetchOnce(streamOf([text]))

    const events: ChatStreamEvent[] = []
    for await (const ev of streamChat({ query: "q" }, new AbortController().signal)) {
      events.push(ev)
    }

    expect(events.map((e) => e.kind)).toEqual(["token", "token", "sources", "meta", "done"])
    expect(events[0]).toEqual({ kind: "token", data: { text: "Once " } })
    expect(events[2]).toMatchObject({ kind: "sources", data: { footer: "Crime and Punishment, Part 1, Chapter 1" } })
    expect(events[3]).toMatchObject({ kind: "meta", data: { cache_hit: false } })
  })

  it("yields token -> sources -> meta -> done in order on the cache path", async () => {
    const text =
      `event: token\ndata: {"text":"Cached answer."}\n\n` +
      `event: sources\ndata: {"sources":[],"footer":""}\n\n` +
      `event: meta\ndata: {"route_name":"CACHE","chunk_ids":[],"ttft_ms":null,"total_ms":5,"filter_relaxed":false,"no_context":true,"cache_hit":true}\n\n` +
      `event: done\ndata: {"complete":true,"finish_reason":"none","refusal":null}\n\n`
    mockFetchOnce(streamOf([text]))

    const events: ChatStreamEvent[] = []
    for await (const ev of streamChat({ query: "q" }, new AbortController().signal)) {
      events.push(ev)
    }

    expect(events.map((e) => e.kind)).toEqual(["token", "sources", "meta", "done"])
    expect(events[2]).toMatchObject({ kind: "meta", data: { cache_hit: true } })
  })

  it("drops an unrecognized event name and a non-event/data record without throwing", async () => {
    const text =
      `event: token\ndata: {"text":"a"}\n\n` +
      `event: future_event\ndata: {"foo":"bar"}\n\n` +
      `: keepalive ping\n\n` +
      `event: sources\ndata: {"sources":[],"footer":""}\n\n`
    mockFetchOnce(streamOf([text]))

    const events: ChatStreamEvent[] = []
    for await (const ev of streamChat({ query: "q" }, new AbortController().signal)) {
      events.push(ev)
    }

    expect(events.map((e) => e.kind)).toEqual(["token", "sources"])
  })

  it("reassembles a record whose \\n\\n separator is split across two fetch-body chunks", async () => {
    const full =
      `event: sources\ndata: {"sources":[],"footer":"mid-record split"}\n\n` +
      `event: meta\ndata: {"route_name":"HYBRID","chunk_ids":[],"ttft_ms":1,"total_ms":2,"filter_relaxed":false,"no_context":false,"cache_hit":false}\n\n`
    // Split mid-record (inside the first record's data line), not at a \n\n
    // boundary -- exercises the buffer accumulation across reader.read() calls.
    const splitPoint = full.indexOf('"footer"')
    const chunk1 = full.slice(0, splitPoint)
    const chunk2 = full.slice(splitPoint)
    mockFetchOnce(streamOf([chunk1, chunk2]))

    const events: ChatStreamEvent[] = []
    for await (const ev of streamChat({ query: "q" }, new AbortController().signal)) {
      events.push(ev)
    }

    expect(events.map((e) => e.kind)).toEqual(["sources", "meta"])
    expect(events[0]).toMatchObject({ data: { footer: "mid-record split" } })
  })

  it("rejects cleanly, without an unhandled rejection, when the signal is already aborted", async () => {
    const controller = new AbortController()
    controller.abort()
    const abortError = new DOMException("aborted", "AbortError")
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(abortError))

    const gen = streamChat({ query: "q" }, controller.signal)
    await expect(gen.next()).rejects.toThrow(abortError)
  })

  it("terminates the generator without an unhandled rejection when the stream errors mid-read (abort)", async () => {
    const encoder = new TextEncoder()
    let pulled = false
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!pulled) {
          pulled = true
          controller.enqueue(encoder.encode(`event: token\ndata: {"text":"a"}\n\n`))
        } else {
          controller.error(new DOMException("aborted", "AbortError"))
        }
      },
    })
    mockFetchOnce(body)

    const gen = streamChat({ query: "q" }, new AbortController().signal)
    const first = await gen.next()
    expect(first.value).toEqual({ kind: "token", data: { text: "a" } })
    await expect(gen.next()).rejects.toThrow()
  })
})
