# 09 — Frontend: the Vue 3 SPA, SSE consumption, the design system

`frontend/` is a Vue 3 + Vite + TypeScript single-page app over the existing `/chat` SSE endpoint. It
adds **no backend component and no GPU consumer** — it is a static build served by the same FastAPI
process ([E21](EVIDENCE.md#E21)). It is the smallest subsystem in the project and this document is
sized accordingly.

---

## 1. Why an SPA, and why this small a one

The product is one page: a transcript, a composer, a scope filter, a health light. There is no second
view and no navigation. The reason it is an SPA at all rather than server-rendered HTML is the
streaming answer — tokens arrive over ~21 s ([E28](EVIDENCE.md#E28)) and have to be appended into a
live DOM node, which is client-side state by definition.

Given that, the stack decisions were all subtractive:

- **Vue 3 with `<script setup>` SFCs + TypeScript.** TypeScript is non-negotiable at the API
  boundary: the SSE event union is the contract with `src/api/app.py`, and it is the only place a
  backend change can break the client silently.
- **No Pinia.** One transcript, one in-flight request, one cached `/books` tree. That is a handful of
  `ref`/`reactive` values behind three composables (`useChat`, `useHealth`, `useBooks`) over one
  plain reactive module. Pinia buys devtools time-travel and module namespacing a single-view solo
  app never spends.
- **No `vue-router`.** One page.
- **No component library.** The visual system is bespoke and small; a kit would fight every token in
  §4 and inflate the bundle. Ten hand-built single-file components, 2,746 lines of tracked source
  total ([E21](EVIDENCE.md#E21)).
- **No background polling.** `/health` is fetched once on mount and re-polled **on demand** after a
  `/chat` 503 or 504 — the only signals that backend state may have changed. A `setInterval` would
  wake llama-server on a timer for a reader who is not reading.

All network access lives in `src/api/*`, which is the app's **only** `fetch` site
([E21](EVIDENCE.md#E21)). Components read reactive state; composables call the boundary.

---

## 2. Consuming the stream

The browser's native `EventSource` **cannot POST**, and `/chat` takes a JSON body. So the client
reads `fetch` → `response.body.getReader()` → `TextDecoder` and parses SSE by hand
([E21](EVIDENCE.md#E21)). That is roughly forty lines in `api/chatStream.ts`, and it is where the
subsystem's real engineering sits, because a hand-rolled stream parser has to survive a network that
does not respect record boundaries.

The parser accumulates decoded text into a buffer and splits on the `\n\n` record separator in a
loop. Each complete record is parsed into a typed event and yielded from an async generator; the
composable consumes it as a `for await`. The hazards it has to handle, each covered by an offline
harness ([E29](EVIDENCE.md#E29)):

| Hazard | Handling |
|---|---|
| A frame splits **mid-record** | the partial stays in the buffer until the rest arrives |
| A frame splits **mid-separator** (`\n` \| `\n`) | same buffer, `indexOf("\n\n")` simply does not match yet |
| One chunk carries **several complete records** | the split loop drains the buffer, not just its first record |
| A **multi-byte codepoint** splits across chunks | `TextDecoder.decode(value, {stream: true})` holds the partial sequence — this is the whole reason the decoder is stateful |
| An **unknown event name** | `parseRecord` returns null and the record is **dropped, not thrown** — a future server event stays non-fatal |
| A **trailing partial record** at stream close | discarded; a half-record is never parsed |

Twelve offline harness cases cover these plus the storage behaviour in §3, all passing
([E29](EVIDENCE.md#E29)).

**Termination.** The live path emits no `done` event and ends by stream close; the cache path emits
`done` (`08-api.md` §2). The consumer handles both by treating `done` as a no-op marker and setting
the turn to `complete` when the loop exits — so the same code path terminates either shape.

**Event reduction.** `token` appends to `turn.answer`; `sources` replaces the turn's source list and
footer; `meta` is stored for the debug row and **never gates rendering** of prose or sources; `error`
sets a terminal `stream-error` state. A user Stop aborts through an `AbortController`, and the turn
becomes `interrupted` with the tokens received so far kept verbatim — never silently re-submitted.

**The defect worth remembering** ([E21](EVIDENCE.md#E21)): each `Turn` must be wrapped in
`reactive()` **before** being pushed into `session.turns`. Vue stores a pushed plain object raw and
only proxies it on read, so every later `turn.answer +=` on the local reference bypassed the set trap
— values correct in memory, DOM never updated. It presented as a permanently blank answer and looked
like a cache regression. **All 19 unit tests were green through it**, because they assert values and
the defect was purely a render notification. No unit test can catch that class of bug; running the
thing is what caught it.

---

## 3. Transcript storage

The transcript is UI-only state, persisted to `localStorage` under one key, written on a 200 ms
debounce, and **never sent back to `/chat`** — v1 has no conversation memory
([E19](EVIDENCE.md#E19)).

Its behaviour under pressure is the part that needed design:

- **A 50-turn soft cap** ([E29](EVIDENCE.md#E29)). Only the tail is serialized; the in-memory array
  is not trimmed.
- **Quota exhaustion drops the oldest and retries**, in a loop, until the payload fits or the array
  is empty. It terminates by construction — each iteration removes an element — and it never throws
  and never blocks the caller. A failed write leaves the in-memory transcript intact, so the current
  session is never damaged by a storage problem.
- **A corrupt or non-array stored value loads as empty** rather than crashing at boot. Storage is a
  cache of the transcript, not its source of truth.

The session id is generated once per page load and used for **log correlation only**.

---

## 4. The design system

"The Evening Reading Room": one dim theme, no light theme in v1, defined as CSS custom properties in
`src/styles/tokens.css` ([E21](EVIDENCE.md#E21)).

The aim was a reading surface, not a chat app. Three rules carry most of it:

**One accent, used rarely.** A single amber — "Lamplight Amber", `--accent #e8a33d` — against warm
near-black surfaces and warm off-white ink ([E21](EVIDENCE.md#E21)). It lights the send control, the
streaming caret, the active-turn marker and the source affordance, and nothing else. Its rarity is
the point; a second accent would make both of them decoration.

**Two type stacks with different jobs.** Spectral serif for the answer prose, `system-ui` sans for
chrome — so the model's output reads as a page and the interface reads as an interface. The reading
measure is capped at 66ch at line-height 1.7 ([E21](EVIDENCE.md#E21)); a full-width answer is
unreadable at any font size.

**Everything on a scale.** A ~1.2-ratio type scale, a 4px spacing base, three radii, exactly one
shadow, and a semantic z-index scale that nothing bypasses ([E21](EVIDENCE.md#E21)). The constraint
is what keeps a hand-built system from drifting into ad-hoc values, which is the failure mode a
component library would otherwise have prevented.

Accessibility basics are in the tokens rather than bolted on: a global 2px accent focus ring, and
motion (150/220 ms on a single easing curve) collapsed under `prefers-reduced-motion`.

**Fonts are self-hosted** — no CDN, no runtime external fetch — via six explicit per-subset,
per-weight imports rather than the barrel import, which is what keeps the bundle at 340 KB across 14
content-hashed font files ([E21](EVIDENCE.md#E21)). Cyrillic 400 and 400i are included because the
corpus is Russian literature and character names appear in both scripts.

**Gap.** The palette's contrast ratios were **computed at design time from OKLCH values and are not
carried in `EVIDENCE.md`**, and no accessibility audit and no Lighthouse or bundle-performance
measurement was ever run beyond the build size ([E30](EVIDENCE.md#E30)). The design intent is
documented; the compliance is not measured.

---

## 5. Build and serve

One build, two serving modes, **the same relative paths in both** — no hardcoded host, no CORS
([E21](EVIDENCE.md#E21)):

- **Production:** `npm run build` → `frontend/dist/`, mounted last by FastAPI
  ([E19](EVIDENCE.md#E19)). One process, one port, one origin.
- **Development:** Vite's own dev server proxying `/chat`, `/health` and `/books` to
  `127.0.0.1:8000`.

Because the FastAPI mount raises at startup when `frontend/dist/` is absent, `npm run build` is
mandatory before **any** uvicorn boot, dev-proxy workflows included (`08-api.md` §1).

Every dependency is pinned exactly, with no ranges ([E21](EVIDENCE.md#E21)). One pin is deliberate
rather than incidental: **TypeScript is held at 5.9.3, not 7.x.** `vue-tsc` type-checks through
TypeScript's JavaScript compiler API, and the 7.0 Go port ships no programmatic API until 7.1, so
`vue-tsc` fails outright on it. Vite never invokes `tsc`, so nothing is lost by holding; the bump
trigger is recorded.

**Verification and its limits.** 19/19 Vitest unit tests, `vue-tsc --noEmit` clean, 12/12 offline
harnesses, and one live full-stack browser round covering streaming, the filter lists, every endpoint
and the llama-server-down 503 ([E21](EVIDENCE.md#E21), [E29](EVIDENCE.md#E29)). Two honest caveats:
the P10 fixes were **never re-run**, so 20/20 is an expectation and not a result, and the acceptance
checklist was never completed ([E30](EVIDENCE.md#E30)).

**Accepted risk:** six `high` npm-audit findings, all in one **dev-only** dependency chain under
`@vue/test-utils`, never in the browser bundle, where `audit fix --force` would downgrade the test
library ([E21](EVIDENCE.md#E21)). Recorded as accepted rather than silently carried.
