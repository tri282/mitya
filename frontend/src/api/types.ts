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
export interface MetaEvent {
  route_name: string
  chunk_ids: (string | number)[] // Qdrant ids (int or uuid)
  ttft_ms: number | null
  total_ms: number
  filter_relaxed: boolean
  no_context: boolean
  cache_hit: boolean
}

export interface ErrorEvent {
  detail: string
}

export type DoneEvent = Record<string, never>

export interface Source {
  book: string
  part: number | null
  chapter: number | null
}

// GET /health
export interface Health {
  llama_server: string
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
