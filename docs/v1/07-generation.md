# 07 — Generation: prompt composition, the persona/grounding seam, decoding, the cache

Retrieval ends with a passages block and a deduped source list ([E16](EVIDENCE.md#E16)). This
document covers what happens to them: how the prompt is composed, why grounding is displayed
programmatically rather than asked of the model, how the sampler is configured and why, and what the
response cache does and does not buy.

Code: `src/rag/generate.py`, `src/rag/cache.py`. Serving-side wiring is `08-api.md`.

---

## 1. The seam

Mitya has two goals that are not merely different but actively opposed.

**Style** comes from a QLoRA fine-tune whose system prompt instructs the model to answer *in the
first person as Dostoevsky* and to give *no editorial comments and references to external sources*.
That instruction is not decoration — it is the conditioning that produces the voice, and it was
trained on rows that **never contained retrieved context at all** ([E17](EVIDENCE.md#E17)).

**Substance** comes from injecting retrieved passages into that same prompt and expecting the model
to use them.

So every injected passage is out-of-distribution for the model that has to consume it, and the most
obvious way to prove grounding — inline `[Book I, Chapter 2]` citations, which the original RAG
design demanded — asks the model to do precisely the thing it was fine-tuned to refuse. The two
subsystems were built independently and meet for the first time inside this prompt. The design
question of this stage is not "how do we get citations", it is **which of the two goals is allowed to
depend on the model's cooperation**.

The answer taken: **persona depends on the model; grounding does not.**

---

## 2. Prompt composition

Composition is a flag, `Config.PROMPT_COMPOSITION`, with two implemented modes
([E17](EVIDENCE.md#E17)).

**Composition A — the shipped default.** The system message opens with the **verbatim fine-tuned
persona head**, byte-identical to `src/training.py`'s `SYSTEM_PROMPT`; then the retrieved passages,
framed as the author's *own recollections* ("Recall these passages from your own novels; let their
substance inform your answer, but never mention passages, sources, or quotations"). The user turn
carries the **question only**.

Two things are doing work in that framing. First, the persona head is byte-identical rather than
paraphrased, because any drift between the training system prompt and the serving system prompt would
be a silent style regression with no test to catch it — the verbatim match is checked, not assumed
([E25](EVIDENCE.md#E25)). Second, "your own recollections" is chosen to sit *with* the
no-external-references conditioning instead of against it: the model is never asked to acknowledge a
source, only to remember. Context stays out of the user turn to keep the prompt shape as close as
possible to what the fine-tune actually saw.

The persona head is **inlined** in `generate.py` rather than imported from `training.py`. That module
pulls unsloth, torch, trl, peft and transformers at import scope; the serving path must never load
them. The duplication is deliberate and its risk — drift between the two copies — is mitigated by the
byte-identity check rather than by an import.

**Composition B** keeps the persona head unmodified and moves **both** the passages and the question
into the user turn. It was added for the P7 A/B and was never deleted.

**The A/B outcome: no winner** ([E24](EVIDENCE.md#E24)). Across 36 queries × 2 compositions, judged
by an LLM on persona retention and groundedness, the two compositions differ by roughly a tenth of a
point on a 1–5 scale, and B fabricates slightly more while scoring slightly higher. The plan had
pre-committed to deleting the loser. There was no loser, so nothing was deleted and both branches
remain behind the flag. This is recorded as a result rather than tidied away: a null result on a
choice the design treated as important is information about the choice — the composition is not where
the quality lives.

Where the quality does live is `10-evaluation.md` and [E25](EVIDENCE.md#E25): the ceiling is the
fine-tune, and groundedness ≈ 2.5 / 5 held on *both* compositions, which is exactly what a
prompt-plumbing problem would not do.

---

## 3. Grounding is programmatic

The Sources list is **not** generated. It is built in code from retrieval metadata — the deduped
`(book, part, chapter)` tuples that `src/rag/context.py` already produced — rendered by
`build_sources_footer()`, and emitted by the API as its own SSE event **after** the token stream ends
([E17](EVIDENCE.md#E17), [E19](EVIDENCE.md#E19)).

**What this buys.** Grounding display cannot be hallucinated. There is no path by which the UI shows
a chapter that retrieval did not return, because the model is never consulted about it. It also means
grounding is *unconditional*: a bad answer still carries an accurate provenance list, so a reader can
see what the model was given and judge the gap themselves. And it removes an entire class of failure
— citation-format enforcement against a model conditioned to refuse citations, which fails
unpredictably and cannot be repaired by retrying.

**What it costs.** There is **no per-sentence attribution**. The list says which chapters were in
context, not which sentence came from which chapter, and nothing detects an answer that ignored the
passages entirely while still displaying them. That is a real weakness, and it is visible in the
evaluation: the judge scores groundedness against the retrieved passages block rather than against
citations, precisely because an inline-citation check would fail by design on every response
([E24](EVIDENCE.md#E24)).

The honest framing: the Sources list is a **provenance display of the retrieval**, not a claim about
the answer. Per-sentence attribution would require either a cooperating model or a post-hoc
sentence-to-passage aligner; neither was built in v1.

Two consequences worth naming. When retrieval returns nothing usable, the footer is empty and the
Sources event carries an empty list rather than a fabricated one ([E20](EVIDENCE.md#E20)). And on the
STRUCTURAL route the context is a chapter summary plus excerpts, so the source list names the
summarized chapter first ([E16](EVIDENCE.md#E16)) — the display follows assembly order, not model
order, because there is no model order to follow.

---

## 4. Decoding

Generation is a ~30-line `httpx` streaming client against llama-server's OpenAI-compatible
`/v1/chat/completions`. LiteLLM was drafted, and already in `requirements.txt`, and removed: a
100-provider abstraction to call one localhost endpoint is a dependency with its own churn and no
payoff ([E17](EVIDENCE.md#E17)).

Settings and their reasons ([E17](EVIDENCE.md#E17)):

- **`max_tokens = 400`** — an answer budget, and a latency budget. Decode is the dominant term in
  total response time ([E28](EVIDENCE.md#E28)).
- **`temperature = 0.6`**, below llama-server's ~0.8 default, and **`top_p = 0.9`**. This is the
  interesting one: it is not a style preference but a **fix for a measured failure**. In the P7 A/B,
  10 of 72 generations returned zero tokens — the weak q4 7B sampling EOS as its first token. The
  failures were per-run, never per-query: the same input succeeded on a re-draw, so this was
  stochastic, not a bad prompt.
- **One empty-completion retry** (`EMPTY_RETRIES = 1`), a re-draw with a fresh seed, fired only when
  an attempt yields *nothing*. Because the guard triggers on zero tokens, a normal stream is never
  buffered and TTFT is unaffected — tokens are yielded live.

That pairing — lower the sampling temperature *and* retry the degenerate draw — is the only part of
the quality ceiling that was fixable at the serving layer ([E25](EVIDENCE.md#E25)). Everything else
in the ceiling traces to the training data.

Transport timeouts are connect 5 s, read 120 s. The read timeout is generous because a cold prompt
prefill on a 6GB card is slow: TTFT measured ~7.9 s against a pre-build budget of 3 s
([E28](EVIDENCE.md#E28)). Failure translation — `LlamaServerUnavailable` / `LlamaServerTimeout` into
503 / 504 — is the API layer's job and is covered in `08-api.md`.

---

## 5. The cache

`src/rag/cache.py` is roughly fifteen lines over `diskcache` (SQLite-backed, `data/cache/`)
([E18](EVIDENCE.md#E18)).

**Keyed on** the normalized query plus the filter dict — exact match only, no TTL (the corpus is
static), storing only successful non-empty answers as `{answer, sources, footer}`.

**Why exact match.** A semantic cache means an embedding index and a similarity threshold, for a
single-user application whose hit rate is near zero anyway. The cache exists as the product's cache
**boundary** — the seam where a real deployment would put something better — and is deliberately kept
trivial. A low hit rate is an accepted outcome here, not a defect. `diskcache` over Redis for the same
reason: no external service for a solo local deploy.

**What a hit is worth.** A cache hit returns in ~0.008 s against a ~21.3 s cold answer
([E28](EVIDENCE.md#E28)). That ratio is why the cache is worth its fifteen lines despite the expected
hit rate: the cost of the miss path is entirely GPU decode, so the one class of repeated query that
does occur — re-asking the same question after a reload — goes from twenty seconds to instant.

**What the cache makes stale or unsafe.**

- **A hit does not stream.** The cached answer is emitted as a *single* `token` event, so it renders
  as one instant block rather than as prose arriving word by word. This is a visible behavioural
  difference between two identical questions and is by design, not a bug — but it means the cache
  changes the product's feel, not just its latency.
- **The stored `sources` and `footer` are frozen at write time.** Re-ingesting the corpus, changing
  the chunking, or editing a chapter summary does not invalidate anything — the key is the query, not
  the index. There is no TTL and no version stamp. In v1 the corpus is static so this never bit; in
  any system where the index changes, the cache key would have to carry an index generation.
- **Cached answers are stochastic samples, permanently.** With `temperature = 0.6` a cached answer is
  one draw frozen forever. The cache therefore also caches whatever was wrong with that draw.
- **Legacy values are handled by contract, not migration.** `cache.get` treats any stored value that
  is not the `{answer, sources, footer}` dict — the bare answer strings written before the UI work —
  as a **miss**, regenerating once and self-healing on the next hit. No migration script, no
  key-namespace bump.

**Gap.** The cache hit rate in use was never measured ([E30](EVIDENCE.md#E30)). A near-zero rate was
predicted at design time and accepted; it was never observed. The speedup figure above is a
per-hit measurement, not a statement about how often hits happen.

**Ordering note.** The semaphore is acquired *before* the cache is checked, so a hit still queues
behind an in-flight generation ([E20](EVIDENCE.md#E20)). With one user submitting sequentially
nothing ever queues, so this costs nothing in v1 — but it means the cache does not, as one might
assume, provide a fast lane around a busy generator.

---

## 6. Where this stage's weaknesses actually are

Stated plainly, because they are the interesting part:

1. **No per-sentence attribution**, by construction — §3.
2. **Prompt composition is not the lever.** Measured, not assumed: no A/B winner
   ([E24](EVIDENCE.md#E24)), and groundedness invariant across compositions ([E25](EVIDENCE.md#E25)).
3. **The persona is prompt-conditioned, and the prompt is long.** The passages block sits inside the
   system message ahead of the question; the estimated context is around 2,000 tokens for three
   chunks ([E16](EVIDENCE.md#E16)). No experiment isolated how much persona drift the injected block
   causes — the A/B moved the block between messages, it never removed it.
4. **The cache is a boundary, not a system** — and its real-world value was never measured
   ([E30](EVIDENCE.md#E30)).
