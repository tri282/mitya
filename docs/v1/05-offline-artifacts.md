# 05 — Offline artifacts: the entity map and the chapter summaries

**What this document answers:** why two LLM-built artifacts sit beside the vector index, why they are
built **once, offline** rather than computed per request, and why one of them has a human review gate
that was not optional.

The shape of both decisions is the same trade. A teacher LLM makes a pass over the whole corpus at
build time; the request path then does a dictionary lookup. The cost is paid once, in API quota and
in review hours, and never again in latency or in GPU contention — which matters here because the
GPU has exactly one resident ([E1](EVIDENCE.md#E1)) and it is the model answering the question.

`scripts/build_entities.py`, `scripts/review_entities.py`, `scripts/fix_entities.py`,
`scripts/check_entity_folds.py`, `scripts/build_summaries.py`.

---

## 1. The entity map: why it exists

The corpus is **Constance Garnett's translations only** ([E5](EVIDENCE.md#E5)), and readers do not
arrive spelling names the way Garnett did. A reader who knows these novels through Pevear and
Volokhonsky types `Dunya`, `Razumikhin`, `Sonya` where the index holds `Dounia`, `Razumihin`,
`Sonia`. Nothing about that query is wrong, and nothing about the index is wrong; they simply do not
share tokens.

Both retrieval legs suffer, asymmetrically. The dense leg absorbs some spelling variance on its own.
The BM25 leg does not — it searches for tokens that occur **nowhere in the corpus**, and contributes
nothing to fusion. The failure is silent: an answer still comes back, grounded in whatever the dense
leg found.

Russian naming makes this worse than ordinary alias handling. One character is legitimately
addressed by given name, patronymic, surname, diminutive, and several diminutives of the diminutive,
across a novel that runs hundreds of pages. A query naming any one of them should reach the same
chapters.

So the map's job is: given a name in a query, return the canonical name plus every alias it is known
by. That resolved set is appended to the query text before embedding — **query expansion, not a
Qdrant filter or a score boost** ([E10](EVIDENCE.md#E10)). The original design called for a `should`
clause on the payload, and that would have been a serious bug: Qdrant filters are binary, a `should`
clause adds no score, and a should-only filter *excludes* every non-matching point. It would have
looked like a boost and behaved like a hard filter, quietly cutting recall.

Purely mechanical spelling variance is handled in code, not data: `src/rag/names.py` applies a
transliteration fold symmetrically to both the index keys and the incoming query
([E11](EVIDENCE.md#E11)). The map holds only what no rule reaches. The line is "derivable vs
irregular" — a rule that earns one win and one false merge belongs in the data, and one such rule was
rejected on exactly that evidence.

---

## 2. How it was built, and why the human gate was not optional

**Extraction is one teacher call per chapter, not per chunk.** 231 chapters against 3,284 points is
roughly a 14× difference in calls ([E8](EVIDENCE.md#E8)), and the free-tier daily quota makes the
per-chunk version not merely
expensive but structurally impossible ([E13](EVIDENCE.md#E13)). Entities are merged per book
afterwards, so chunk-level granularity would have bought nothing anyway.

The machine pass covered **231 / 231 chapters with zero failures**, and produced **678 people**
([E10](EVIDENCE.md#E10)). That number is the argument for the gate. The teacher over-extracts in two
directions at once, and both are invisible in the raw file:

- **It emits an entry for anyone named in passing** — one-off mentions sit alphabetically beside the
  principal cast with nothing to distinguish them.
- **It fragments one person across several keys**, because the text calls them different things in
  different chapters.

And it produces aliases that are actively destructive downstream. The clearest case: the teacher gave
one entry the aliases `He`, `Him`, `Lord` — which match every pronoun in the book. Harmless in a raw
extraction; catastrophic at query time, where an alias is what a name query matches on. One such
entry drags in every chapter ever written.

**None of that is detectable by a script alone, and none of it is safely fixed by one.** Deciding
whether two keys are the same person is a judgment about the novel. So the gate is human, and the
tooling's job is only to make the reading order right: `review_entities.py` writes a triage report
ranking entries by **actual mention counts in the novel text** (so rare entries sort to the bottom)
and pairing **merge candidates** by alias collision and name containment (so fragments of one person
land adjacent rather than hundreds of entries apart). It never writes the reviewed artifact — that stays a
human act by construction.

The pass went **678 people down to 256 keys** through the human gate and the scripted curation that
followed ([E10](EVIDENCE.md#E10)).

**Three disciplines around the gate, each of which paid for itself:**

- **The machine artifact and the reviewed artifact are separate files.** `build_entities.py` writes
  `entities.raw.json` and *never* `entities.json`. Re-running extraction cannot clobber hand
  corrections — the one failure mode that would have made the whole gate worthless.
- **Every teacher response is logged verbatim** to a JSONL file, so a merge fix costs a code change
  and a `--remerge` with **zero API calls**. This exists because it was learned the hard way: an
  earlier key-space bug cost a full re-run of every billed call, precisely because the only copies of
  those extractions were already fused inside the merged output.
- **Curation is a script with explicit operation dicts and timestamped backups**, not dozens of hand
  edits. Deterministic, reversible, and reviewable as a diff.

**The schema had to change, and the reason is the interesting part.** The design specified a flat
`{name: {canonical, aliases, book}}`, which assumes a reference name identifies one person across
five novels. It does not — Katerina Ivanovna is two different women in two different books, and so
are Nikolay, Dmitri, Marya and Lizaveta ([E10](EVIDENCE.md#E10)). The flat shape had nowhere to put
the second person, so the merge fused them: one record carrying one novel's label and the other
novel's canonical name. The schema became one record **per book** per name, and resolution became
book-scoped — with a hard book filter, take the matching candidate; without one, more than one
candidate means the name does not identify a person, so **expansion is skipped rather than guessed**.
Expanding with the wrong novel's aliases is worse than not expanding.

**The gate has a machine check behind it.** `check_entity_folds.py` fails on a **same-book
collision** and passes cross-book ones — the exact criterion the resolution rule can honour, since
book-scoped resolution cannot separate two identities inside one novel but the filter separates them
across novels. It exited clean with zero same-book collisions ([E10](EVIDENCE.md#E10)).

**Not measured:** entity-map coverage. There is no recall or precision number against a ground-truth
character list; the human ground truth was written and the comparison never computed
([E10](EVIDENCE.md#E10), [E30](EVIDENCE.md#E30)). Nor is the reviewed artifact under version control
— the curation exists on disk with two backup files as its entire history, and `--remerge` can
replay the machine pass but not the human gate. That is the sharpest unmitigated risk in this
document.

---

## 3. Chapter summaries: the query family chunk-RAG cannot serve

**"What happens in chapter 5?"** has no answer in a top-k over chunks, and no amount of retrieval
tuning changes that. The answer is distributed across an entire chapter; every individual chunk is a
fragment of it, and selecting the three best fragments produces three excerpts, not a summary.
This is a structural limitation of chunk retrieval, not a ranking failure
([E12](EVIDENCE.md#E12)).

Since the corpus is static and finite, the fix is to pre-compute the thing that cannot be retrieved:
one neutral summary per chapter, keyed by the same canonical `(book, part, chapter)` tuple the chunk
payloads use, so the router can look one up directly. Coverage is **231 / 231 chapters with zero
gaps** against the structure key space ([E12](EVIDENCE.md#E12)) — full parity is what lets the
STRUCTURAL route treat a resolvable scope as a guaranteed hit, with a fallback kept only as defense in
depth.

**Why a teacher LLM and not Mitya.** Two reasons, either sufficient. Mitya would summarize in
persona voice, and these summaries are *internal context* — a first-person Dostoevskian gloss is the
wrong input to a grounding step. And generating 231 of them would put the served model on the GPU for
a build-time job, which is the exact contention this artifact exists to avoid.

**Two chapters the teacher refused**, both narrating violence or sexual coercion, and both recovered
through a fallback prompt ([E12](EVIDENCE.md#E12)). Two findings from that worth keeping:

- The refusal category sits **above** the configurable safety settings and cannot be disabled, and it
  is deterministic — so a retry is not a retry, it is a billed repeat of a known failure. Refusals
  became fatal-and-logged rather than retried.
- The block is **output-side, not input-side**: the same two chapters pass *entity extraction*
  untouched. Narrating the events trips the filter; extracting facts from them does not. This is
  worth knowing before designing any pipeline that assumes a blocked document is a blocked document.

The fallback prompt stayed a fallback rather than becoming the default: the other 229 came from the
narrative prompt, and switching would mean re-running all 231 for register consistency
([E12](EVIDENCE.md#E12)).

**Not measured:** the summary path's answer quality. The evaluation set has two rows checking that
the STRUCTURAL route *fires*, and **zero** rows measuring whether the answers it produces are good
([E30](EVIDENCE.md#E30)). The route is verified; the artifact behind it is not.

---

## 4. The teacher model, and the one bakeoff v1 ran

Both artifacts depend on a teacher, and choosing it is **the only competitive model selection v1 ran**
([E13](EVIDENCE.md#E13)). Seven models over two fixed chapters — one entity extraction, one chapter
summary — for **ten real API calls total**. The pass criterion was deliberately mechanical: does the
response parse as strict JSON, and how long did it take.

That criterion is the point. Both jobs are structured-output batch jobs over a fixed corpus; a model
that writes chain-of-thought before its JSON is not "worse at literature", it is unusable for this
without a parser workaround, and the local Gemma models failed exactly there.

**The selection was not made on quality or latency. It was made on quota.** The winner was the only
free-tier text model whose *daily* request limit could absorb a 231-chapter pass at all — every other
candidate on that tier is capped at a fraction of it ([E13](EVIDENCE.md#E13)). Latency was the
tiebreaker among models that were already viable. Naming the binding constraint honestly is better
than retrofitting a quality story onto a quota decision.

Three operational findings came out of the same period and are recorded in
[E13](EVIDENCE.md#E13): the SDK's default automatic retry turns one rate-limited call into several
billed ones and was disabled; a consecutive-429 circuit breaker distinguishes "daily quota gone" from
"slow down"; and two builder processes must never run concurrently, because each holds its own rate
limiter and sees only its own traffic. The operating rule that came out of it: **the provider
dashboard is ground truth, local counters are not**.

---

## 5. The trade, restated

| | Cost | Where it is paid |
|---|---|---|
| Entity map | 231 teacher calls + a human review gate + curation scripts | Build time, once |
| Chapter summaries | 231 teacher calls | Build time, once |
| Both, at request time | one dictionary lookup each | Nothing measurable |

Neither artifact appears in the latency budget ([E28](EVIDENCE.md#E28)); they load once at FastAPI
startup and are resident thereafter. That is what a static corpus buys, and it is why the expensive,
slow, human-reviewed option was affordable here and would not be over changing data.

What the trade does **not** buy is any claim that either artifact is good. Neither has a coverage or
quality number ([E30](EVIDENCE.md#E30)). The entity layer's one attempted attribution — the
`no-expansion` arm of the ablation — was later withdrawn as noise (`11-ablation.md`,
[E27](EVIDENCE.md#E27)). As v1 closed, the honest statement is that the entity map is well-built,
well-reviewed, and of unmeasured value.
