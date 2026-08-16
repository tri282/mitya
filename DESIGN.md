<!-- Settled visual design. Built: the tokens below live as CSS custom properties in frontend/src/styles/tokens.css. -->
---
name: Mitya
description: A dim, warm reading room for talking with a book-grounded Dostoevsky.
colors:
  surface: "#16130f"       # oklch(0.18 0.012 60) — Study Dark, body surface
  panel: "#211d17"         # oklch(0.24 0.012 60) — second layer
  panel-2: "#2a251d"       # oklch(0.28 0.013 60) — hover/active tonal step
  ink: "#ede7dc"           # oklch(0.92 0.015 75) — Page Ink, reading text
  ink-muted: "#aaa08d"     # oklch(0.72 0.015 70) — Muted Ink
  accent: "#e8a33d"        # oklch(0.78 0.13 70) — Lamplight Amber
  accent-hover: "#f2b455"  # oklch(0.83 0.13 75)
  accent-active: "#d5902f" # oklch(0.72 0.13 68)
  accent-muted: "#8a7f6c"  # oklch(0.58 0.04 70) — disabled amber
  on-accent: "#16130f"     # = surface
typography:
  reading: "\"Spectral\", Georgia, \"Times New Roman\", serif"
  chrome: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif"
---

# Design System: Mitya

## 1. Overview

**Creative North Star: "The Evening Reading Room"**

Mitya is a private study late in the evening: one warm lamp, a dark room, a serif page open in front of you, and Dostoevsky answering across the desk. The surface is a warm near-black with a single amber lamplight accent; the reading — long in-character answers — is set in a literary serif and given room to breathe. Everything else (labels, filters, controls, the source list) is quiet sans that recedes. The atmosphere is carried entirely by type, measure, and warm low light, never by texture or period props.

This system explicitly rejects four things. It is not a generic assistant chat (no default bubbles, gray canvas, or blue send button). It is not a SaaS or startup landing page (no warm-beige near-white body, tracked eyebrow kickers, gradient accents, or conversion energy). It is not a costume "olde" theme (no fake parchment, torn edges, blackletter, or quill-cursor kitsch). And it is not neon/cyber dark (no terminal green, glow, or HUD styling — the dark is a dim room, not a machine).

**Key Characteristics:**
- Warm near-black reading surface, one amber lamplight accent used sparingly (≤10%).
- Literary serif for the reading; quiet humanist sans for all chrome.
- Print-heritage restraint (Penguin Classics / NYRB / a well-set novel), not app decoration.
- Unhurried: motion conveys state only, never performs.
- Grounding (book / part / chapter) always present, never louder than the voice.

## 2. Colors

Restrained: a warm dark-neutral field carrying a single amber accent. Tokens below are settled and live as CSS custom properties in `frontend/src/styles/tokens.css`. Contrast ratios are computed from the OKLCH values, not yet instrument-measured on a rendered page — the by-eye AA check on the real theme remains a build-time gate.

### Primary
- **Lamplight Amber** `--accent` `oklch(0.78 0.13 70)` / `#e8a33d` (hover `#f2b455`, active `#d5902f`, disabled/muted `#8a7f6c`; on-accent text `#16130f`): the single accent. Primary action (send), the active/current turn, and the source-link affordance. Nothing decorative. ≈8.6:1 against Study Dark.

### Neutral
- **Study Dark** `--surface` `oklch(0.18 0.012 60)` / `#16130f`: the room — the warm near-black body surface.
- **Panel** `--panel` `oklch(0.24 0.012 60)` / `#211d17`, with `--panel-2` `oklch(0.28 0.013 60)` / `#2a251d` as the hover/active tonal step off it — for the composer, source panel, and tooltips.
- **Page Ink** `--ink` `oklch(0.92 0.015 75)` / `#ede7dc`: the reading text color; ≈15.0:1 against Study Dark.
- **Muted Ink** `--ink-muted` `oklch(0.72 0.015 70)` / `#aaa08d`: secondary labels, source metadata, debug values; ≈7.2:1 against Study Dark — still legible, never light-gray-for-elegance.

### Named Rules
**The One Lamp Rule.** The amber accent lights ≤10% of any screen. Its rarity is what makes it read as a lamp rather than a highlighter.

## 3. Typography

**Reading Font:** Spectral (SIL OFL, self-hosted), weights 400/500/600 + 400 italic. Fallback stack `"Spectral", Georgia, "Times New Roman", serif`.
**Chrome Font:** system humanist sans, no webfont: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`.

**Character:** A contrast-axis pairing — a warm literary serif that makes long answers feel like a printed page, against a plain humanist sans that lets controls disappear. Never Inter or a geometric grotesk for chrome — it reads SaaS.

### Type scale (fixed rem, ratio ~1.2, root = 16px)

| Token | rem / px | Use | Family |
|---|---|---|---|
| `--fs-display` | 2.0 / 32 | app title / empty-state prompt | serif |
| `--fs-title` | 1.5 / 24 | (reserved) section headline | serif |
| `--fs-reading` | 1.1875 / 19 | answer prose | serif |
| `--fs-body` | 1.0 / 16 | input text, question line | sans |
| `--fs-label` | 0.875 / 14 | filters, buttons, source rows | sans |
| `--fs-meta` | 0.8125 / 13 | debug values, health tooltip | sans/mono |

**Weights:** reading 400 (italic 400 for in-text emphasis); labels 500; send button 600; display 500.
**Line-heights:** reading `1.7`; body `1.5`; labels/meta `1.4`.

### Hierarchy
- **Display / Headline** (serif, `--fs-display`): app title / empty-state prompt. Restrained, print title-page scale, not a hero.
- **Reading Body** (serif, `--fs-reading`, line-height `1.7`, 62–70ch measure): the in-character answer prose. This is the surface's main event.
- **Title** (sans, `--fs-title`): section and panel labels.
- **Body / Label** (sans, `--fs-body`/`--fs-label`): filters, buttons, input placeholder, source metadata.

### Named Rules
**The Page Measure Rule.** Answer prose column is `max-width: 66ch` (within the required 62–70ch band) and never exceeds ~70ch. The reading is the product; a full-width wall of serif is not reading.

## 4. Elevation, Spacing & Motion

Flat by default. Depth comes from tonal layering (`--surface` → `--panel` → `--panel-2`) and warm light, not from drop shadows. The one permitted non-tonal effect is a soft warm ambient glow tied to state (focus ring, the Reading Lamp below) — never a hard box-shadow. If any shadow is used it is `0 1px 24px oklch(0.78 0.13 70 / 0.06)` (warm, ambient, near-invisible).

### Named Rules
**The Flat-Room Rule.** Surfaces are flat at rest. Any elevation is a quiet tonal shift or a soft warm glow tied to state (focus, active turn), never decorative shadow.

### Spacing scale
4px base: `--sp-1: 4px` `--sp-2: 8px` `--sp-3: 12px` `--sp-4: 16px` `--sp-5: 24px` `--sp-6: 32px` `--sp-7: 48px` `--sp-8: 64px`. Vertical rhythm between turns uses `--sp-7`; within a turn (question → answer → sources) `--sp-4`/`--sp-5`.

### Radii
`--radius-sm: 4px` (chips, source rows), `--radius-md: 8px` (composer, panels), `--radius-lg: 12px` (the composer container). No fully-round pills — that reads chat-app.

### Motion
Duration 150–250ms, exponential ease-out `cubic-bezier(0.16, 1, 0.3, 1)`. State-only, never decorative or looping-for-flair: token stream (no per-token animation — only the caret animates), focus ring fade-in, send press, and a one-time source-list reveal (fade + rise) when grounding lands. `prefers-reduced-motion: reduce` collapses all of the above to instant state changes — always honored.

### The signature element — The Reading Lamp
A single soft warm amber glow at the streaming caret: while Mitya writes, a small amber bar breathes (opacity 0.5→1, ~1.6s ease-in-out) — the lamp is lit while he speaks, and fades out on completion. This doubles as the honest "thinking/writing" indicator — no spinner anywhere in the system. Under reduced motion it is a static amber block that disappears on completion. Boldness is spent here and nowhere else.

## 5. Components

Settled component list; per-component state styling (default/hover/focus/active/disabled/error) lives in each component's scoped `<style>` block under `frontend/src/components/`.

- **AppHeader** — app title + `HealthIndicator`. No nav, no actions.
- **HealthIndicator** — subtle `/health` status dot (`ok`/`degraded`/`unknown`); event-driven, no polling loop.
- **Transcript** — the scroll region; maps turns to `QuestionLine`/`AnswerBlock`, shows `EmptyState` when empty.
- **QuestionLine** — the user's question, set quiet (sans, muted ink) with an optional per-book scope chip.
- **AnswerBlock** — the reading itself: streaming serif prose, then `SourcesList` and `DebugRow`. The main event.
- **SourcesList** — programmatic grounding (book/part/chapter); present on every completed answer, live or cached.
- **Composer** — textarea + `FilterControl` + send; owns input, emits submit.
- **FilterControl** — cascading book → part → chapter scope, driven by `GET /books`.
- **DebugRow** — collapsed per-answer diagnostics (route, chunk ids, latency, cache hit).
- **EmptyState** — teaches the interface before the first turn; real copy, not decoration.

## 6. Do's and Don'ts

### Do:
- **Do** keep answer prose in the reading serif at a ~62–70ch measure with generous line-height.
- **Do** confine the amber accent to primary action, the active turn, and source affordances (≤10% of the screen).
- **Do** build depth from tonal layering and warm light; keep surfaces flat at rest.
- **Do** verify reading and label text hold ≥4.5:1 against the dark surface, by eye against the real theme.
- **Do** keep motion to state changes only (token stream, hover, focus, send).

### Don't:
- **Don't** ship a generic assistant chat — no default bubbles, gray canvas, or blue send button.
- **Don't** drift toward a SaaS/startup landing page — no warm-beige near-white body, tracked eyebrow kickers, gradient accents, or conversion energy.
- **Don't** use costume "olde" kitsch — no fake parchment textures, torn edges, blackletter display, or quill-cursor skeuomorphism.
- **Don't** go neon/cyber — no terminal green, glowing accents, or sci-fi HUD dark styling.
- **Don't** let light-gray "elegant" text drop below AA; bump toward the ink end instead.
