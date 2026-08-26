// Phase 10 (tests): useChat's turn-state machine. streamChat is mocked so
// each test drives a controlled event sequence and asserts the resulting
// Turn shape/state -- this is the state machine that breaks silently (a
// wrong terminal state, a dropped `sources` re-emission on a cache hit)
// with no visual signal short of staring at the transcript.

import { beforeEach, describe, expect, it, vi } from "vitest"
import type { ChatStreamEvent } from "../api/types"

const { streamChatMock, repollMock } = vi.hoisted(() => ({
  streamChatMock: vi.fn(),
  repollMock: vi.fn(),
}))

vi.mock("../api/chatStream", () => ({
  streamChat: streamChatMock,
  ChatHttpError: class ChatHttpError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail)
    }
  },
}))

vi.mock("./useHealth", () => ({ repoll: repollMock }))

import { ChatHttpError } from "../api/chatStream"
import { session } from "../state/session"
import { useChat } from "./useChat"

/** Wrap a fixed event list as the async generator streamChat's mock returns. */
async function* sequence(events: ChatStreamEvent[]): AsyncGenerator<ChatStreamEvent> {
  for (const ev of events) yield ev
}

beforeEach(() => {
  session.turns.splice(0, session.turns.length)
  streamChatMock.mockReset()
  repollMock.mockReset()
})

describe("useChat", () => {
  it("normal completion: token events accumulate into answer, terminal state is complete", async () => {
    streamChatMock.mockReturnValue(
      sequence([
        { kind: "token", data: { text: "Hello, " } },
        { kind: "token", data: { text: "friend." } },
        { kind: "sources", data: { sources: [], footer: "" } },
        {
          kind: "meta",
          data: {
            request_id: "0123456789abcdef0123456789abcdef",
            route_name: "HYBRID",
            chunk_ids: [1],
            ttft_ms: 100,
            total_ms: 500,
            filter_relaxed: false,
            no_context: true,
            cache_hit: false,
            cost_usd: 0,
            cost_metered: false,
          },
        },
      ]),
    )

    const { send, turns, isStreaming } = useChat()
    await send("a question")

    expect(turns[0].answer).toBe("Hello, friend.")
    expect(turns[0].state).toBe("complete")
    expect(isStreaming.value).toBe(false)
  })

  it("mid-stream error event: terminal state is stream-error with the server detail retained", async () => {
    streamChatMock.mockReturnValue(
      sequence([
        { kind: "token", data: { text: "partial" } },
        { kind: "error", data: { detail: "llama-server dropped mid-generation" } },
      ]),
    )

    const { send, turns } = useChat()
    await send("q")

    expect(turns[0].state).toBe("stream-error")
    expect(turns[0].errorDetail).toBe("llama-server dropped mid-generation")
    expect(turns[0].answer).toBe("partial")
  })

  it("aborted mid-stream: terminal state is interrupted and prior tokens are retained", async () => {
    async function* aborted(): AsyncGenerator<ChatStreamEvent> {
      yield { kind: "token", data: { text: "partial answer" } }
      throw new DOMException("aborted", "AbortError")
    }
    streamChatMock.mockReturnValue(aborted())

    const { send, turns, isStreaming } = useChat()
    await send("q")

    expect(turns[0].state).toBe("interrupted")
    expect(turns[0].answer).toBe("partial answer")
    expect(isStreaming.value).toBe(false)
  })

  it("cache-hit sequence (token -> sources -> meta -> done) populates sources/footer -- no silent regression to no-grounding", async () => {
    streamChatMock.mockReturnValue(
      sequence([
        { kind: "token", data: { text: "Cached answer." } },
        {
          kind: "sources",
          data: {
            sources: [{ book: "Crime and Punishment", part: 1, chapter: 1 }],
            footer: "Crime and Punishment, Part 1, Chapter 1",
          },
        },
        {
          kind: "meta",
          data: {
            request_id: "fedcba9876543210fedcba9876543210",
            route_name: "CACHE",
            chunk_ids: [],
            ttft_ms: null,
            total_ms: 5,
            filter_relaxed: false,
            no_context: false,
            cache_hit: true,
            cost_usd: 0,
            cost_metered: false,
          },
        },
        { kind: "done", data: {} },
      ]),
    )

    const { send, turns } = useChat()
    await send("q")

    expect(turns[0].state).toBe("complete")
    expect(turns[0].sources.length).toBeGreaterThan(0)
    expect(turns[0].footer).toBe("Crime and Punishment, Part 1, Chapter 1")
    expect(turns[0].meta?.cache_hit).toBe(true)
  })

  it("503 http error: terminal state is error-503, detail retained, health is re-polled", async () => {
    async function* http503(): AsyncGenerator<ChatStreamEvent> {
      throw new ChatHttpError(503, "llama-server unavailable")
    }
    streamChatMock.mockReturnValue(http503())

    const { send, turns } = useChat()
    await send("q")

    expect(turns[0].state).toBe("error-503")
    expect(turns[0].errorDetail).toBe("llama-server unavailable")
    expect(repollMock).toHaveBeenCalledOnce()
  })

  it("400 http error: terminal state is error-400, health is NOT re-polled", async () => {
    async function* http400(): AsyncGenerator<ChatStreamEvent> {
      throw new ChatHttpError(400, "unmapped book")
    }
    streamChatMock.mockReturnValue(http400())

    const { send, turns } = useChat()
    await send("q")

    expect(turns[0].state).toBe("error-400")
    expect(repollMock).not.toHaveBeenCalled()
  })
})
