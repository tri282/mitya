# 03 — Corpus Parsing and Chunk Metadata

**What this document is:** why the RAG layer parses each novel with its own rule set instead of
splitting text generically, what `book/part/chapter` metadata buys downstream, the per-book variation
the parser had to absorb, and why a Russian corpus read in English translation needs a name
normalization and transliteration layer. Figures live in [`EVIDENCE.md`](EVIDENCE.md).

Chunking, embedding and the Qdrant collection are [`04-indexing.md`](04-indexing.md); this document
stops at the structural unit and the payload.

---

## 1. The input, and why it is not the fine-tune's input

The RAG layer parses **clean plaintext** of the five novels, Constance Garnett throughout
([E5](EVIDENCE.md#E5)). This is a deliberately different ingest from the fine-tune's OCR'd PDFs
([E2](EVIDENCE.md#E2)), for a reason that follows from what each is for: a garbled word in a training
chunk is diluted across thousands of pairs, while a garbled word in a retrieved passage is shown to
the user and quoted into the model's context. Retrieval has no tolerance for OCR noise, so it does
not accept any.

The single-translator corpus is a simplification with a consequence that §5 has to pay for.

---

## 2. Why a per-book structural parser instead of generic splitting

A generic character or token splitter over a novel produces spans with **no identity**. It can return
a passage; it cannot say where the passage is from. Everything the system does with a retrieved chunk
beyond pasting it into a prompt requires an answer to "which chapter is this".

Three concrete capabilities depend on it, and none of them survives without it:

| Capability | What the metadata does |
|---|---|
| **Hard filtering** | The UI's book/part/chapter filter becomes a Qdrant `must` filter, so "in *The Idiot*, part 2" searches only that region ([E15](EVIDENCE.md#E15)) |
| **The Sources list** | Grounding is displayed *programmatically*, deduped on `(book, part, chapter)`, because the fine-tune was trained to refuse inline citations ([E16](EVIDENCE.md#E16), [E17](EVIDENCE.md#E17)) |
| **The STRUCTURAL route** | "What happens in chapter N" is answered from a pre-generated chapter summary, keyed by the same `(book, part, chapter)` triple ([E12](EVIDENCE.md#E12), [E14](EVIDENCE.md#E14)) |

The Sources case is the strongest argument. Because the model cannot be asked for citations, the
citation has to be reconstructed from the retrieved chunk's payload after the fact. **A chunk with no
structural identity cannot be cited at all** — the whole grounding-display design collapses to
"trust the model", which is precisely what the fine-tune's conditioning makes impossible.

The chapter boundary also earns its place as a **chunking boundary**: chunks never cross a chapter
([E6](EVIDENCE.md#E6)). A window spanning two chapters is unattributable, and it would blend two
scenes into one embedding.

The parser emits `data/rag/structure.json`, which is **three artifacts at once** — the parser's own
verification tripwire, the data behind `GET /books` that populates the filter UI, and the key space
that the summary set must cover exactly ([E5](EVIDENCE.md#E5)).

---

## 3. The variation the parser had to absorb

Five novels, five heading conventions, no shared regex. The parser is a small rule table with one
handler per book, and every scheme below was **read off the actual files** during phase 1 by grepping
heading lines — not assumed from an edition's table of contents ([E5](EVIDENCE.md#E5)). The result is
**231 chapters across 5 books** ([E5](EVIDENCE.md#E5)).

Three discoveries did real work, and each would have produced silent damage if guessed:

1. **The Idiot has no `CHAPTER` keyword anywhere** — zero grep matches. Its chapters are bare roman
   numerals, so `structure.json` records `chapter_label: null` for them ([E5](EVIDENCE.md#E5)). A
   parser written against a `CHAPTER` pattern would have found *one* unit for the entire novel.
2. **The Brothers Karamazov nests differently and resets numbering per book.** There is also no
   `Book I.` heading in the file: Book I is inferred as everything preceding `Book II.`, and the book
   count is read from the file rather than hardcoded ([E5](EVIDENCE.md#E5)).
3. **Crime and Punishment's epilogue is a trap.** Unless the last part's content range is explicitly
   clipped at the `EPILOGUE` heading, the final chapter silently swallows the epilogue
   ([E5](EVIDENCE.md#E5)). The epilogue is then mapped to its own part number rather than discarded.

The failure mode common to all three is worth naming: **a structural parser that is wrong does not
raise.** It returns fewer, larger, mislabeled units, and everything downstream keeps working while
returning subtly wrong answers. That is why the total unit count was checked against a manual
reference count as a tripwire at parse time ([E5](EVIDENCE.md#E5), [E29](EVIDENCE.md#E29)), and why
the parse was worth its own phase rather than being a preamble to indexing.

A related lesson about metadata that is *allowed* to be null: `chapter_label: null` for The Idiot was
correct in the data, declared as a non-null literal in the frontend's types, and rendered to the user
as the string `"null 5"` ([E21](EVIDENCE.md#E21)). Structural metadata crosses several layers, and a
nullable field has to be nullable in every one of them.

---

## 4. What ends up on a chunk

Each indexed point carries `{book, part, chapter, canonical_entities, text}` plus a derived
normalized field for exact matching ([E8](EVIDENCE.md#E8)). Point ids are stable deterministic
integers over (book order × unit order × window order), so a re-ingest of an unchanged corpus
produces the same ids ([E6](EVIDENCE.md#E6)) — which is what makes the `chunk_ids` logged per request
comparable across runs.

`canonical_entities` is the offline entity map's contribution to the payload. It is worth being
precise about how it is *not* used at request time: the original design boosted matching points via a
Qdrant `should` clause, and that was abandoned because **Qdrant filters are binary** — a `should`
clause adds no score, and a should-only filter *excludes* non-matching points, which would have
silently gutted recall ([E10](EVIDENCE.md#E10)). Entity information is instead applied on the query
side, as query expansion (§5). The payload field is retained for evaluation and future client-side
boosting.

---

## 5. Names: normalization and transliteration folds

### 5.1 Why a Russian corpus in English translation needs this layer

The corpus is Garnett only, and readers know these novels through **many** translators. A
Pevear/Volokhonsky reader types `Dunya`, `Razumikhin`, `Sonya`; Garnett wrote `Dounia`, `Razumihin`,
`Sonia`. Without a fold, the entity never resolves, no expansion happens, and — worse — **the BM25 leg
searches tokens that occur nowhere in the corpus** ([E11](EVIDENCE.md#E11)). The sparse leg does not
degrade gracefully here; it contributes nothing at all, and the failure is invisible in aggregate.

`src/rag/names.py` provides two derived match keys, applied **symmetrically** to both the match index
and the incoming query, with the true `canonical` spelling never overwritten
([E11](EVIDENCE.md#E11)):

- `normalize()` — casefold, diacritic strip, ligature expansion, and separator collapse over
  whitespace, hyphens, apostrophes **and sentence/quote punctuation**.
- `fold()` — `normalize()` plus an ordered transliteration ruleset covering the standard English
  transliteration disagreements (щ variants, `kh`↔`h`, `zh`↔`j`, `ou`↔`u`, `y`↔`i`, ё, Garnett's
  French `c`, German-tradition forms, doubled-consonant collapse). Rule order is load-bearing:
  several rules manufacture input for later ones.
- `fold_tokens()` — matching on **token boundaries**, never substrings.

Fold output is not human-readable (`fold("Count") == "kunt"`), which is harmless precisely because it
is applied to both sides and never surfaces; expansion appends the verbatim `canonical`, never a fold
key ([E11](EVIDENCE.md#E11)).

### 5.2 The code/data line

The line is **derivable versus irregular**, not mechanical versus semantic. Anything a rule reaches
is code, including `Rogojin`↔`Rogozhin`; only what no rule reaches is data —
`Muishkin`↔`Myshkin`, `Shigalov`↔`Shigalyov`, `Ferdishenko`↔`Ferdyshchenko`
([E11](EVIDENCE.md#E11)).

One rule was **rejected on evidence** and it is the best illustration of the line: `ui→i` catches
`Muishkin`↔`Myshkin`, but it also folds "Luise Ivanovna" into "Lise", merging three distinct
characters. A rule earning one win and one false merge belongs in the data, so Muishkin became a
curated alias ([E11](EVIDENCE.md#E11)). A false merge in a name layer is worse than a miss: a miss
returns nothing, a merge returns the wrong person's chapters, confidently.

### 5.3 Two bugs this layer produced, both instructive

- **The `?`-terminated name.** `What happens to Dunya?` must fold to the same token as bare `Dunya`,
  or the trailing mark strands the last token and the whole-token lookup misses. Until this was fixed
  during the phase-4 eval run, **most natural questions were silently not expanding**
  ([E11](EVIDENCE.md#E11)). This is exactly the class of defect that returns plausible results rather
  than raising.
- **Substring matching does not travel.** `fold_tokens()` exists because `liza ⊂ lizaveta` resolves
  "Lizaveta Prokofyevna" to "Liza". The entity map used it correctly; the router, one file over,
  matched book aliases with a plain `in`, so "demons" matched inside "demonstration"
  ([E11](EVIDENCE.md#E11)).

**Book titles are a separate map**, not part of the entity file, and `resolve_book_title()`
deliberately **raises on an unmapped title** rather than passing it through
([E11](EVIDENCE.md#E11)). The reasoning generalizes to this whole layer: an unmapped title becomes a
hard filter matching zero points — a silent empty result, which is worse than a loud 400.

### 5.4 Resolution when a name is ambiguous

Reference names are not unique across five novels — Katerina Ivanovna, Nikolay, Dmitri, Marya and
Lizaveta all denote different people in different books. The entity map is therefore keyed
`{name: [record per book]}` rather than flat, after a flat schema was observed fusing two people into
one record ([E10](EVIDENCE.md#E10)). The resolution rule at request time is exact: with a hard book
filter, take the matching book's record; **with no filter and more than one candidate, skip expansion
rather than guess** ([E10](EVIDENCE.md#E10)) — expanding with the wrong novel's aliases is worse than
not expanding.

The entity map itself, its teacher-LLM extraction and its human curation gate, is
[`05-offline-artifacts.md`](05-offline-artifacts.md).

---

## 6. What would break without this layer

Removing the structural parse and its metadata, and keeping generic splitting:

- **The Sources list becomes impossible**, and with it the only grounding signal the user gets, since
  inline citations are ruled out by the fine-tune ([E17](EVIDENCE.md#E17)).
- **The book/part/chapter filter has nothing to filter on** ([E15](EVIDENCE.md#E15)).
- **The STRUCTURAL route disappears entirely** — chapter summaries are keyed by the parse, and chunk
  retrieval structurally cannot answer "what happens in chapter N", because that answer is spread
  across a whole chapter and no top-k over chunks reconstructs it ([E12](EVIDENCE.md#E12)).
- **Chunks straddle chapter boundaries**, blending two scenes into one embedding
  ([E6](EVIDENCE.md#E6)).
- **The evaluation set cannot exist.** Every row is labeled with exactly one gold
  `(book, part, chapter)`, and `recall@k` is defined as whether that triple appears in the top-k
  ([E22](EVIDENCE.md#E22), [E23](EVIDENCE.md#E23)). Without the parse there is no label space, and
  the project has no measurement of its retrieval at all.

Removing the name layer instead: entity expansion silently stops firing on non-Garnett spellings and
the sparse leg searches absent tokens — a diffuse quality loss with no error and no log line. The
eval set carries deliberate cross-translation tripwire rows for exactly this reason, so a regression
lands on identifiable rows rather than as an aggregate drop ([E22](EVIDENCE.md#E22)).

---

## 7. Gaps

- **No entity-map coverage number.** A human ground-truth character list was written for measuring
  under-extraction, and the comparison was never computed ([E10](EVIDENCE.md#E10),
  [E30](EVIDENCE.md#E30)).
- **The eval set is Garnett-heavy** — only two of its rows exercise the cross-translation folding
  that this layer exists for, so aggregate recall measures the name layer on the wrong workload
  ([E22](EVIDENCE.md#E22)).
- **No OCR or parse error rate** for the corpus text itself beyond the unit-count tripwire
  ([E30](EVIDENCE.md#E30)).
