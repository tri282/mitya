// Phase 4 (api-boundary): hand-rolled SSE-over-fetch parser for POST /chat.
// The browser cannot POST an EventSource (design §I.5), so this reads
// res.body through a TextDecoder, accumulates into a buffer, and splits on
// the SSE record separator "\n\n" by hand.

import type {
  ChatRequest,
  ChatStreamEvent,
  DoneEvent,
  ErrorEvent,
  MetaEvent,
  SourcesEvent,
  TokenEvent,
} from "./types"

export class ChatHttpError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail)
  }
}

/** Parse one "event:"/"data:" SSE record into its typed variant. Unknown
 * event names are dropped (return null), not thrown — this is what lets a
 * future event the client doesn't know yet stay non-fatal (design §IV.2). */
function parseRecord(record: string): ChatStreamEvent | null {
  let event: string | null = null
  let data: string | null = null
  for (const line of record.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim()
    } else if (line.startsWith("data:")) {
      data = line.slice("data:".length).trim()
    }
  }
  if (event === null || data === null) return null

  switch (event) {
    case "token":
      return { kind: "token", data: JSON.parse(data) as TokenEvent }
    case "sources":
      return { kind: "sources", data: JSON.parse(data) as SourcesEvent }
    case "meta":
      return { kind: "meta", data: JSON.parse(data) as MetaEvent }
    case "done":
      return { kind: "done", data: JSON.parse(data) as DoneEvent }
    case "error":
      return { kind: "error", data: JSON.parse(data) as ErrorEvent }
    default:
      return null
  }
}

export async function* streamChat(
  req: ChatRequest,
  signal: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const res = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({ detail: res.statusText }))) as { detail: string }
    throw new ChatHttpError(res.status, body.detail)
  }

  const reader = res.body!.getReader()
  const decoder = new TextDecoder("utf-8")
  let buffer = ""

  while (true) {
    const { done, value } = await reader.read()
    if (done) break // discard any trailing partial record (§I.5)
    buffer += decoder.decode(value, { stream: true })

    let sep: number
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const record = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const parsed = parseRecord(record)
      if (parsed !== null) yield parsed
    }
  }
}
