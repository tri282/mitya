// Phase 4 (api-boundary): SSE/chat/health/books response types.
// Mirrors the backend one-to-one (design §I.4). Concrete everywhere — no
// `any`/`unknown` at this boundary (DECIDE C2).

// Requests
// NOTE (DECIDE, seed 3 / C3): backend ChatFilters.part/.chapter are INTEGERS,
// but the /books tree encodes part keys as STRING integers ("1","2",...). The
// request builder MUST coerce part/chapter to number (parseInt) when
// composing filters; book stays a string (backend resolves the title). Drop
// unset fields (do not send nulls) so the backend's exclude_none contract is
// honored. Coercion itself happens at the call site (Phase 7's
// FilterControl) — this type only documents the contract.
export interface ChatFilters {
  book?: string
  part?: number
  chapter?: number
}

export interface ChatRequest {
  query: string
  filters?: ChatFilters
  session_id?: string
}

// SSE event payloads (event name -> data shape)
export interface TokenEvent {
  text: string
}

export interface SourcesEvent {
  sources: Source[]
  footer: string
}

// NEW — see design §IV.
// Mirrors src/gateway/ladder.py:24-44, one-for-one. Only three are reachable
// on a meta-emitting path: UNAVAILABLE ("503") is an HTTP 503 and carries no
// meta at all, CHEAPER is unreachable by construction (ladder.py:123-126, one
// generation arm), and REFUSED is a per-attempt outcome next_rung() never
// returns. The other three are typed because the wire can carry them the day
// the ladder gains an arm; nothing in this client branches on them.
export type Rung =
  | "primary_model"
  | "cheaper_model"
  | "cached_answer"
  | "retrieval_only"
  | "content_refused"
  | "503"

export interface MetaEvent {
  // P1 step 1's join key. Paste it into Jaeger and the whole stage tree
  // resolves -- it IS the OTel trace id in hex, not a parallel identifier.
  // Server has emitted this since step 1; this interface had drifted.
  request_id: string
  // P1 step 4, corrected at P5 (R102 rider 2 -- this comment described the
  // retired header). The AUTHENTICATED tenant, resolved server-side from the
  // session cookie once per request (C11). The X-Tenant-ID header P1 read is
  // ignored: an authenticated tenant a header can override is not
  // authenticated. `/chat` requires a session, so this is never the anonymous
  // default on a successful response.
  tenant: string
  // P1 step 9 (D9). The exact serving config / corpus version that produced
  // this answer -- 12 hex chars each, content-addressed
  // (src/config_artifact.py). Every answer traces to the config that made
  // it, not just the route it took.
  config_version: string
  corpus_version: string
  route_name: string
  chunk_ids: (string | number)[] // Qdrant ids (int or uuid)
  ttft_ms: number | null
  total_ms: number
  filter_relaxed: boolean
  no_context: boolean
  cache_hit: boolean
  // True when this request carried recent turns, profile rows or episodic
  // digests, and therefore neither READ nor WROTE the answer cache
  // (app.py:687-732). It is why a memory-bearing request always reports
  // cache_hit:false; without it the bypass and an ordinary miss are the same
  // bytes. Renders beside cache_hit because the two are one reading.
  memory_bearing: boolean
  // Ladder tier this request was served at (app.py:817, :1041, :1300).
  rung: Rung
  // The producing arm (app.py:812, :1298). `null` on retrieval_only and ONLY
  // there: nothing generated, so there is no arm to name (app.py:1028-1031).
  // This field, not any static string, is what the UI may say about which
  // model answered -- the local arm is off the request path (A3).
  model: string | null
  // Live generation path only (app.py:1291-1292); ABSENT on the cached and
  // retrieval-only branches, and null when the provider reported no usage.
  // Absent stays absent -- a 0 here would read as a free draw rather than an
  // unreported one.
  prompt_tokens?: number | null
  completion_tokens?: number | null
  // NOT TYPED, deliberately: meta.context_plan (app.py:1301) is
  // ContextPlan.attributes(), an allocator-internal shape that
  // docs/v2/08-memory.md §4 keeps as a seam. It is dropped by the same
  // unknown-field rule that makes every field above safe to add.
  // P1 step 3. `null` means UNPRICED -- the serving path could not obtain
  // token counts -- and is NOT the same as 0. `cost_metered: false` means
  // nobody bills for these tokens (local inference), so the figure is honest
  // but is not a price: display it as provisional, never as a cost.
  cost_usd: number | null
  cost_metered: boolean
}

export interface ErrorEvent {
  detail: string
}

// v3 P0: closed sets mirrored from src/gateway/contract.py and src/gateway/ledger.py.
export type FinishReason = "stop" | "length" | "content_filter" | "error" | "other" | "none"
export type SpendLayer =
  | "per_call"
  | "per_request"
  | "tenant_day"
  | "global_minute"
  | "global_day"
  | "global_month"

// A spend layer declined the request. `retry_after_s` is null when the same
// request would be declined again (per_call, per_request).
export interface Refusal {
  layer: SpendLayer
  retry_after_s: number | null
}

// Terminal event, sent on every branch.
export interface DoneEvent {
  complete: boolean
  finish_reason: FinishReason
  refusal: Refusal | null
}

export interface Source {
  book: string
  part: number | null
  chapter: number | null
}

// GET /health
export interface Health {
  // Still the wire shape, so still typed -- but no longer DISPLAYED (§3.6).
  // Its hosted value is the literal string "not serving generation
  // (provider=...)" (app.py:531-536), which is true and useless.
  llama_server: string
  // app.py:524-528. The arm that actually serves. This is what the degraded
  // tooltip narrates now: A3 froze the local arm out of the request path, so
  // llama_server is a subsystem line, not the answer to "what served this".
  generation: { model: string; provider: string; status: string }
  qdrant: { collection: string; count: number } | string
  artifacts: { entities_loaded: number; summaries_loaded: number; structure_loaded: number }
  status: "ok" | "degraded"
}

// GET /books — structure.json tree, served verbatim. CONCRETE shape (DECIDE,
// seed 2 — verified against data/rag/structure.json; 5 books). Typed end to
// end, no `unknown`.
export interface PartNode {
  chapters: number[] // flat int list of chapter numbers in this part
  unit_count: number
}

export interface BookNode {
  file: string
  scheme: "part_chapter" | "book_chapter"
  part_label: "Part" | "Book" // VARIES per book — Karamazov uses "Book"
  // NULL for books whose chapters are bare roman numerals with no "CHAPTER"
  // keyword in the source text (The Idiot, Notes from the Underground -- see
  // that node's `notes`). Renderers fall back to DEFAULT_CHAPTER_LABEL; the
  // chapters exist and are numbered, only the word is missing.
  chapter_label: "Chapter" | null
  notes: string | null
  parts: Record<string, PartNode> // keys are STRING integers ("1","2",...)
  total_units: number
}

export type StructureTree = Record<string, BookNode> // key = book display name

// chatStream.ts's discriminated union over the SSE event stream.
export type ChatStreamEvent =
  | { kind: "token"; data: TokenEvent }
  | { kind: "sources"; data: SourcesEvent }
  | { kind: "meta"; data: MetaEvent }
  | { kind: "done"; data: DoneEvent }
  | { kind: "error"; data: ErrorEvent }
