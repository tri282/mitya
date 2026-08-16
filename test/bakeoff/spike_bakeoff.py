"""Throwaway validation spike: compare Gemini-family models on the two phase-3
teacher tasks (entity extraction, chapter summary) over 2 real corpus chapters.

NOT a phase implementation. Writes a human-readable report to test/bakeoff/.
Nothing here touches data/rag/*.json or src/.

Budget: 10 API calls total.
  chapter A -> entity extraction on all 7 models      (7 calls)
  chapter B -> chapter summary on the 3 two-call models (3 calls)
"""

import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import google.generativeai as genai

from src.config import Config
from src.rag.ingest import Unit, parse_all

logger = logging.getLogger(__name__)

REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "RESULTS.md")
RAW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_results.json")

SEED = 42
MIN_TOKENS, MAX_TOKENS = 2000, 6000  # keep chapters under the Gemma 16K TPM budget


@dataclass
class ModelSpec:
    label: str
    model_id: str
    calls: int
    is_gemma: bool


# Ids resolved against the live account via genai.list_models(), not guessed.
# "Gemini 3 Flash" on the AI Studio dashboard has no GA id -- only the preview.
MODELS = [
    ModelSpec("Gemini 2.5 Flash",      "models/gemini-2.5-flash",       1, False),
    ModelSpec("Gemini 2.5 Flash Lite", "models/gemini-2.5-flash-lite",  1, False),
    ModelSpec("Gemini 3 Flash Preview", "models/gemini-3-flash-preview", 1, False),
    ModelSpec("Gemini 3.1 Flash Lite", "models/gemini-3.1-flash-lite",  2, False),
    ModelSpec("Gemini 3.5 Flash",      "models/gemini-3.5-flash",       1, False),
    ModelSpec("Gemma 4 26B A4B IT",    "models/gemma-4-26b-a4b-it",     2, True),
    ModelSpec("Gemma 4 31B IT",        "models/gemma-4-31b-it",         2, True),
]

# Mirrors src/generation.py Generator._call_gemini. Non-negotiable for this corpus:
# Dostoevsky's text is violent (axe murder, parricide, suicide) and default filters
# block it, which makes resp.text raise and silently burns quota.
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
]

ENTITY_PROMPT_TMPL = """You are performing neutral analytical text processing, not roleplay.

Read the chapter text below (from the novel "{book}") and extract every PERSON
mentioned by name or clear alias. Do NOT include places, organizations, or
abstract concepts. For each distinct person, merge all name variants/aliases
you find into one entry.

Return STRICT JSON ONLY (no markdown fences, no commentary) in EXACTLY this shape:

{{
  "Raskolnikov": {{"canonical": "Rodion Romanovich Raskolnikov",
                  "aliases": ["Rodya", "Rodka", "Rodion"],
                  "book": "Crime and Punishment"}}
}}

The top-level key is a short reference name for the person. "canonical" is the
fullest form of their name found in the text. "aliases" lists other name
forms/diminutives/nicknames used for them in the text (may be an empty list).
"book" must be exactly: "{book}"

CHAPTER TEXT:
---
{text}
---

Return only the JSON object."""

SUMMARY_PROMPT_TMPL = """You are performing neutral analytical text processing, not roleplay.
Do NOT write in a literary or in-character voice. Write a plain, factual,
neutral summary as if for an internal index, not for a reader of the novel.

Summarize what happens in the chapter below, in roughly 100-150 words,
covering plot events and which characters are involved. Neutral factual
register only.

Return STRICT JSON ONLY (no markdown fences, no commentary) in EXACTLY this shape:

{{"{key}": "your summary text here"}}

CHAPTER TEXT:
---
{text}
---

Return only the JSON object."""


def approx_tokens(text: str) -> int:
    return len(text) // 4


def unit_key(u: Unit) -> str:
    return f"{u.book}|{u.part}|{u.chapter}"


def pick_chapters(units_by_book: dict[str, list[Unit]]) -> tuple[Unit, Unit]:
    """One C&P chapter (part+chapter scheme), one BK chapter (book+chapter scheme).
    Random but seeded, so a re-run hits the same text and stays comparable."""
    rng = random.Random(SEED)

    def eligible(book: str) -> list[Unit]:
        pool = [u for u in units_by_book[book] if MIN_TOKENS <= approx_tokens(u.text) <= MAX_TOKENS]
        if not pool:
            raise RuntimeError(f"no chapter in {book} within {MIN_TOKENS}-{MAX_TOKENS} tokens")
        return pool

    return rng.choice(eligible("Crime and Punishment")), rng.choice(eligible("The Brothers Karamazov"))


def call_model(spec: ModelSpec, prompt: str) -> dict:
    """One API call. Returns a result dict; never raises. Gemma is served through the
    Gemini API but does not accept safety_settings, so it falls back to a bare call."""
    model = genai.GenerativeModel(spec.model_id)
    t0 = time.monotonic()
    try:
        if spec.is_gemma:
            resp = model.generate_content(prompt)
        else:
            resp = model.generate_content(prompt, safety_settings=SAFETY_SETTINGS)
    except Exception as e:
        return {"raw": None, "latency": time.monotonic() - t0,
                "error": f"{type(e).__name__}: {e}", "blocked": None}
    latency = time.monotonic() - t0

    # resp.text raises when the response was blocked or truncated -- surface why
    # rather than crashing, since a blocked call still consumed quota.
    try:
        raw = resp.text
    except Exception as e:
        finish = None
        feedback = None
        try:
            finish = str(resp.candidates[0].finish_reason)
        except Exception:
            pass
        try:
            feedback = str(resp.prompt_feedback)
        except Exception:
            pass
        return {"raw": None, "latency": latency,
                "error": f"resp.text raised {type(e).__name__}: {e}",
                "blocked": f"finish_reason={finish} prompt_feedback={feedback}"}

    return {"raw": raw, "latency": latency, "error": None, "blocked": None}


def check_json(raw: str | None) -> tuple[bool, str]:
    """Strict parse, then a fence-stripped parse purely as a diagnostic."""
    if raw is None:
        return False, "no output"
    try:
        json.loads(raw.strip())
        return True, "strict parse ok"
    except json.JSONDecodeError:
        pass
    stripped = raw.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
        body = body.rsplit("```", 1)[0]
        try:
            json.loads(body.strip())
            return False, "FAILS strict parse; parses only after stripping markdown fences"
        except json.JSONDecodeError:
            pass
    return False, "FAILS strict parse (and not a fence issue)"


def main() -> None:
    genai.configure(api_key=Config.GOOGLE_API_KEY)

    units_by_book = parse_all()
    chap_a, chap_b = pick_chapters(units_by_book)
    logger.info("chapter A: %s (~%d tokens)", unit_key(chap_a), approx_tokens(chap_a.text))
    logger.info("chapter B: %s (~%d tokens)", unit_key(chap_b), approx_tokens(chap_b.text))

    entity_prompt = ENTITY_PROMPT_TMPL.format(book=chap_a.book, text=chap_a.text)
    summary_prompt = SUMMARY_PROMPT_TMPL.format(key=unit_key(chap_b), text=chap_b.text)

    # Build the call plan up front so the budget is auditable before spending it.
    plan: list[tuple[ModelSpec, str, str]] = []
    for spec in MODELS:
        plan.append((spec, "entity_extraction", entity_prompt))
        if spec.calls >= 2:
            plan.append((spec, "chapter_summary", summary_prompt))
    logger.info("call plan: %d calls across %d models", len(plan), len(MODELS))

    results = []
    for i, (spec, task, prompt) in enumerate(plan):
        logger.info("[%d/%d] %s | %s", i + 1, len(plan), spec.label, task)
        out = call_model(spec, prompt)
        json_ok, json_note = check_json(out["raw"])
        results.append({
            "model_label": spec.label, "model_id": spec.model_id, "task": task,
            "chapter": unit_key(chap_a if task == "entity_extraction" else chap_b),
            "json_ok": json_ok, "json_note": json_note, **out,
        })
        if out["error"]:
            logger.warning("  error: %s", out["error"])
        else:
            logger.info("  ok in %.1fs, json_ok=%s", out["latency"], json_ok)

        if i < len(plan) - 1:
            # Gemma is capped at 16K TPM; two ~5K-token calls land close to it.
            time.sleep(60 if spec.is_gemma else 5)

    with open(RAW_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    write_report(results, chap_a, chap_b)
    logger.info("report written: %s", REPORT_PATH)


def write_report(results: list[dict], chap_a: Unit, chap_b: Unit) -> None:
    lines = [
        "# Phase 3 teacher-model bake-off",
        "",
        f"Chapter A (entity extraction): **{unit_key(chap_a)}** (~{approx_tokens(chap_a.text)} tokens)",
        f"Chapter B (chapter summary): **{unit_key(chap_b)}** (~{approx_tokens(chap_b.text)} tokens)",
        "",
        f"Total API calls: **{len(results)}**",
        "",
        "## Summary table",
        "",
        "| Model | Task | Strict JSON | Latency | Error |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        err = (r["error"] or "-")[:60]
        lat = f"{r['latency']:.1f}s" if r["latency"] else "-"
        lines.append(f"| {r['model_label']} | {r['task']} | {'yes' if r['json_ok'] else 'NO'} | {lat} | {err} |")

    lines += ["", "## Raw outputs", ""]
    for r in results:
        lines += [f"### {r['model_label']} — {r['task']}", "",
                  f"- id: `{r['model_id']}`", f"- chapter: `{r['chapter']}`",
                  f"- json: {r['json_note']}", f"- latency: {r['latency']:.1f}s"]
        if r["error"]:
            lines += [f"- **error**: `{r['error']}`"]
        if r["blocked"]:
            lines += [f"- **blocked**: `{r['blocked']}`"]
        lines += ["", "```json", (r["raw"] or "(no output)"), "```", ""]

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    main()
