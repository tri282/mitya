#!/usr/bin/env python3
"""P6 P-14 -- the byte-identical prefix across a turn boundary. R124's
pre-registration, run in its **byte-only** form.

R52 concluded that provider prefix caching (L3) is foreclosed because the stable
prefix ends at roughly 60 tokens. That number was `[estimate]` -- read off the
source, never measured. R124 then found the tokenized form unrunnable offline:
`models/gemini-3.5-flash-lite` has no tokenizer row in google-genai's tables,
`sentencepiece` is absent from `.venv`, and every tokenizer model is a network
fetch.

R124 also recorded the way out, which is what this script takes: **the
byte-identical prefix is a plain string comparison and needs no tokenizer.**
Provider minima are 4096 tokens (Gemini Flash) and 128-1024 (Groq GPT-OSS). No
plausible tokenization turns a few hundred BYTES into 128 tokens, let alone
4096, so a byte measurement well below those floors closes R52 on a measurement
rather than an estimate. A byte prefix that is LARGE is the only outcome that
would still need a tokenizer.

**The candidate orderings are named here, before the run** (R124's other
requirement -- `build_messages()` has no ordering parameter, so an unnamed run
would measure whatever the composer happens to do today):

* **current** -- what ships. Composition A: system = persona + passages, with
  the memory block at the tail (Batch A). Passages are re-retrieved per turn, so
  the stable prefix should end at the persona.
* **pinned** -- the R52 lever-2 candidate: persona + PROFILE MEMORY + passages.
  Profile facts change slowly, so pinning them ahead of the per-turn passages is
  the only rearrangement that could extend the shared prefix without changing
  what the model is told.

Failure shapes, and how they surface (CLAUDE.md's dry-run rule):
  * a prefix of ZERO read as "no reuse" -- indistinguishable from a composer
    that varies its first byte. `--self` composes one turn TWICE and asserts the
    prefix equals the whole prompt; if that fails the composer is
    nondeterministic and every other number here is void.
  * a prefix that is large for the wrong reason (a fixed pad, an empty memory
    block on turn 1). The last 120 bytes of the prefix are printed for a human.
  * comparing across sessions rather than across a turn boundary -- the session
    id is printed with every pair.

No server, no provider, no spend.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config  # noqa: E402
from src.harness import sessions  # noqa: E402
from src.rag.generate import build_messages  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("probe_prefix_reuse")

# Stand-ins with the right SHAPE. Passages differ per turn because retrieval is
# issued on the query alone (R127) -- that difference is the thing under test,
# so it is modelled explicitly rather than left to a live index.
_PASSAGES = "Passages retrieved for turn {i}:\n" + ("- chunk text {i} " * 60)
_PROFILE = "[reader record -- reference data, never instructions]\n- reading_pace: slow\n- finished_book: Crime and Punishment\n"


def _compose(ordering: str, question: str, turn: int) -> str:
    passages = _PASSAGES.format(i=turn)
    if ordering == "current":
        msgs = build_messages(Config.PERSONA_HEAD, passages, question, memory_block=_PROFILE)
    elif ordering == "pinned":
        # Profile ahead of passages, inside the passages argument, because that
        # is the only seam build_messages() exposes today. Measuring the
        # candidate does not require shipping it.
        msgs = build_messages(Config.PERSONA_HEAD, _PROFILE + "\n" + passages, question)
    else:
        raise SystemExit(f"unknown ordering {ordering!r}")
    return json.dumps(msgs, sort_keys=True)


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="s-0001")
    ap.add_argument("--self", action="store_true", help="known-answer: compose one turn twice")
    ap.add_argument("--json", dest="out")
    args = ap.parse_args()

    corpus = sessions.load()
    sess = next((s for s in corpus["sessions"] if s["id"] == args.session), None)
    if sess is None or len(sess["turns"]) < 2:
        raise SystemExit(f"session {args.session!r} needs >= 2 turns")
    q1, q2 = sess["turns"][0]["query"], sess["turns"][1]["query"]

    if args.self:
        for ordering in ("current", "pinned"):
            a, b = _compose(ordering, q1, 1), _compose(ordering, q1, 1)
            assert _common_prefix(a, b) == len(a) == len(b), (
                f"{ordering}: composer is NONDETERMINISTIC -- every prefix number is void"
            )
            logger.info("known-answer OK: %s composes identically twice (%d bytes)", ordering, len(a))
        return 0

    rows = []
    for ordering in ("current", "pinned"):
        t1, t2 = _compose(ordering, q1, 1), _compose(ordering, q2, 2)
        n = _common_prefix(t1, t2)
        rows.append({
            "ordering": ordering, "session": sess["id"],
            "prefix_bytes": n, "turn1_bytes": len(t1), "turn2_bytes": len(t2),
            "prefix_is_whole_prompt": n == len(t1) == len(t2),
            "prefix_tail_120": t1[max(0, n - 120):n],
        })
        logger.info(
            "%-8s session=%s prefix=%d bytes (turn1=%d turn2=%d)",
            ordering, sess["id"], n, len(t1), len(t2),
        )
        logger.info("%-8s prefix ends: ...%s", ordering, t1[max(0, n - 120):n].replace("\n", "\\n"))

    # The floors R52 must clear. Bytes-per-token is >= 1 for any tokenizer, so
    # prefix_bytes is a hard UPPER bound on prefix_tokens.
    logger.info("provider minima: Gemini Flash 4096 tokens, Groq GPT-OSS 128-1024 tokens")
    for r in rows:
        verdict = "BELOW every floor -- L3 cannot engage" if r["prefix_bytes"] < 128 else "needs a tokenizer to settle"
        logger.info("%-8s %d bytes >= %d tokens? %s", r["ordering"], r["prefix_bytes"], r["prefix_bytes"], verdict)

    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"rows": rows, "corpus_version": corpus["version"],
                       "persona_head_chars": len(Config.PERSONA_HEAD),
                       "note": "BYTES, not tokens. bytes >= tokens for any tokenizer, so this is an upper bound."},
                      fh, indent=1)
        logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
