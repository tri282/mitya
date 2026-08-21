# src/rag/teacher.py
"""Teacher-LLM client for offline artifact builds (phase 3).

Deliberately NOT built on src/generation.py Generator, which cannot do this job:
it fixes its model at __init__, has no rate limiting, and swallows every
exception into a bare "" -- so a caller cannot distinguish a 429 (wait and
retry) from a 404 (stop now). That is fine for its own job (best-effort Q&A pair
generation, where a dropped pair costs nothing) but wrong for a 231-chapter
metered batch. Generator serves the fine-tune pipeline; this module serves RAG.

Rate limiting is the whole point of this module. Free-tier Gemini enforces
requests-per-minute, tokens-per-minute and requests-per-day independently, and
the google-genai SDK's default behaviour actively works against staying inside
them: it retries 429s four extra times on its own (_RETRY_ATTEMPTS = 5,
_RETRY_HTTP_STATUS_CODES includes 429), and every one of those attempts is
billed against the same quota that just ran out. We therefore disable SDK-side
retries entirely (attempts=1) and own pacing + retry here, so every HTTP request
is one this module decided to make and the quota accounting stays honest.
"""

import json
import logging
import re
import time
from collections import deque
from typing import Any, Callable

from google import genai
from google.genai import errors, types

from src.config import Config

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0

# Transient -- worth another attempt. Everything else (404 dead model, 400 bad
# request, 403 bad key) is fatal: retrying it just burns quota to fail again.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Candidate finish reasons that arrive as HTTP 200 with no text and will never
# succeed on retry -- the model refused the content itself. PROHIBITED_CONTENT
# is Gemini's non-configurable filter: it sits above the four HARM_CATEGORY_*
# settings below, reports no safety_ratings, and BLOCK_NONE cannot reach it.
FATAL_FINISH_REASONS = frozenset({"PROHIBITED_CONTENT", "BLOCKLIST", "SPII"})

# Dostoevsky is violent (axe murder, parricide, suicide). Default filters block
# these chapters, and a blocked response has no .text -- it raises, after the
# request has already been charged. Mirrors src/generation.py.
SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold="BLOCK_NONE")
    for c in (
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_HARASSMENT",
    )
]

_RETRY_DELAY_RE = re.compile(r"retry.?delay.*?(\d+)", re.IGNORECASE | re.DOTALL)


class TeacherError(RuntimeError):
    """Call failed and should not be retried by the caller."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ContentBlocked(TeacherError):
    """The model refused this prompt's content (HTTP 200, empty candidate).

    Not retryable and not maskable: the filter that produces it is outside
    safety_settings. The fix is a different prompt shape or a hand-written
    artifact, not another attempt.
    """


class QuotaExhausted(TeacherError):
    """Daily request budget spent. Re-run tomorrow; builders resume from disk.

    Raised by the local counter (which only knows about requests *this process*
    made) and by builders when the server keeps returning 429 -- quota spent by
    an earlier run is invisible to us until the API says so.
    """


def estimate_tokens(text: str) -> int:
    """Rough char/4 heuristic, only used to reserve TPM budget *before* a call.
    Actual usage is trued up from usage_metadata afterwards."""
    return max(1, len(text) // 4)


class RateLimiter:
    """Sliding-window limiter over requests/min, tokens/min and requests/day.

    clock/sleep are injectable so the window logic is testable without burning
    real time or real quota.
    """

    def __init__(
        self,
        rpm: int = Config.TEACHER_RPM,
        tpm: int = Config.TEACHER_TPM,
        rpd: int = Config.TEACHER_RPD,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rpm, self.tpm, self.rpd = rpm, tpm, rpd
        self._clock, self._sleep = clock, sleep
        self._window: deque[list[float]] = deque()  # [timestamp, tokens]
        self.requests_today = 0

    def _evict(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= WINDOW_SECONDS:
            self._window.popleft()

    def _tokens_in_window(self) -> int:
        return int(sum(entry[1] for entry in self._window))

    def acquire(self, tokens: int) -> None:
        """Block until a request of `tokens` fits inside every limit."""
        if tokens > self.tpm:
            raise TeacherError(
                f"single request needs ~{tokens} tokens, over the {self.tpm} tokens/min "
                f"limit; it can never be scheduled"
            )
        if self.requests_today >= self.rpd:
            raise QuotaExhausted(
                f"daily limit of {self.rpd} requests reached; re-run after quota "
                f"resets -- completed work is already on disk and will be skipped"
            )

        while True:
            now = self._clock()
            self._evict(now)
            if len(self._window) < self.rpm and self._tokens_in_window() + tokens <= self.tpm:
                break
            # Oldest entry leaving the window is the soonest anything frees up.
            wait = WINDOW_SECONDS - (now - self._window[0][0]) + 0.05
            logger.debug(
                "rate limit: %d/%d req, %d/%d tok in window; sleeping %.1fs",
                len(self._window), self.rpm, self._tokens_in_window(), self.tpm, wait,
            )
            self._sleep(max(wait, 0.05))

        self._window.append([self._clock(), float(tokens)])
        self.requests_today += 1

    def true_up(self, actual_tokens: int) -> None:
        """Replace the last estimate with the real count from usage_metadata."""
        if self._window:
            self._window[-1][1] = float(actual_tokens)

    def remaining_today(self) -> int:
        return max(0, self.rpd - self.requests_today)


def _retry_delay_from(err: errors.APIError) -> float | None:
    """Gemini 429s carry the server's own retry_delay; prefer it over guessing."""
    try:
        match = _RETRY_DELAY_RE.search(str(err.details))
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None


class TeacherClient:
    """Rate-limited, JSON-mode Gemini client for offline artifact generation."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = Config.TEACHER_MODEL,
        limiter: RateLimiter | None = None,
        max_retries: int = Config.TEACHER_MAX_RETRIES,
        timeout_ms: int = Config.TEACHER_TIMEOUT_MS,
        max_output_tokens: int = Config.TEACHER_MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
    ):
        key = api_key or Config.GOOGLE_API_KEY
        if not key:
            raise TeacherError("GOOGLE_API_KEY is not set; add it to .env")

        # This client speaks the google-genai SDK and nothing else. Non-Google
        # ids exist in Config.MODEL_LIMITS as a quota registry (charter D1
        # wants >= 2 provider families for v2 serving) but have no transport
        # yet -- fail here with the reason rather than at call time with a
        # confusing 404 from the wrong endpoint.
        provider = Config.limits_for(model).get("provider")
        if provider not in (None, "google"):
            raise TeacherError(
                f"model {model!r} is provider {provider!r}; TeacherClient only speaks "
                "google-genai. It is registered for its quota row, not yet wired."
            )

        self.model = model
        self.limiter = limiter or RateLimiter()
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens

        self._client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=timeout_ms,
                # attempts=1 -> no SDK-side retries. See module docstring: the
                # default of 5 turns one rate-limited call into five billed ones.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        # response_mime_type is what makes strict JSON reliable. Without it,
        # models wrap output in markdown fences or emit a whole reasoning trace
        # before the JSON (both Gemma variants did exactly this in the bake-off).
        self._config = types.GenerateContentConfig(
            safety_settings=SAFETY_SETTINGS,
            response_mime_type="application/json",
            # 0.0 everywhere in production. Injectable only so the judge-variance
            # probe (P0 step 9a) can separate "the judge is deterministic" from
            # "the rubric is unambiguous" -- at temperature 0 those two look
            # identical, and they demand different fixes.
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        logger.info(
            "teacher: model=%s limits=%d rpm / %d tpm / %d rpd, sdk retries disabled",
            self.model, self.limiter.rpm, self.limiter.tpm, self.limiter.rpd,
        )

    @staticmethod
    def _finish_reason(resp: types.GenerateContentResponse) -> str | None:
        try:
            reason = resp.candidates[0].finish_reason
        except Exception:
            return None
        return getattr(reason, "name", None) or str(reason)

    @staticmethod
    def _prompt_feedback(resp: types.GenerateContentResponse) -> str | None:
        try:
            return str(resp.prompt_feedback)
        except Exception:
            return None

    def _extract_text(self, resp: types.GenerateContentResponse) -> str:
        # Read the finish reason before touching .text: a refused response is a
        # 200 whose .text is simply empty, so without this the diagnosis is lost
        # and the caller retries a call that can only fail the same way.
        finish = self._finish_reason(resp)
        if finish in FATAL_FINISH_REASONS:
            raise ContentBlocked(
                f"model returned no content: finish_reason={finish}; "
                f"safety_settings cannot override this filter"
            )

        try:
            text = resp.text
        except Exception as e:
            raise TeacherError(
                f"no text in response ({type(e).__name__}: {e}); "
                f"finish_reason={finish} prompt_feedback={self._prompt_feedback(resp)}"
            ) from e
        if not text:
            raise TeacherError(
                f"empty response text (finish_reason={finish} "
                f"prompt_feedback={self._prompt_feedback(resp)})"
            )
        return text

    def generate_json(self, prompt: str) -> Any:
        """One logical call: pace, send, parse. Retries only what is retryable.

        Returns whatever JSON the model produced -- object or array. It does not
        reliably honour the shape the prompt asks for: at temperature 0 it picks
        one deterministically per chapter, and for ~1/3 of the corpus it wraps
        each requested object in a single-element array. Callers own the shape
        check, since only they know what they asked for.

        Raises TeacherError on fatal failure, QuotaExhausted when the day is spent.
        """
        reserve = estimate_tokens(prompt) + self.max_output_tokens
        last_err: str | None = None
        last_status: int | None = None

        for attempt in range(1, self.max_retries + 1):
            self.limiter.acquire(reserve)
            try:
                resp = self._client.models.generate_content(
                    model=self.model, contents=prompt, config=self._config
                )
            except errors.APIError as e:
                if e.code not in RETRYABLE_STATUS:
                    # 404/400/403 will fail identically forever -- stop immediately.
                    raise TeacherError(f"fatal API error {e.code}: {e.message}", status=e.code) from e
                last_err = f"{e.code}: {e.message}"
                last_status = e.code
                if attempt == self.max_retries:
                    break
                delay = _retry_delay_from(e) or min(2.0 ** attempt, 60.0)
                logger.warning(
                    "attempt %d/%d got %s; sleeping %.1fs",
                    attempt, self.max_retries, last_err, delay,
                )
                time.sleep(delay)
                continue

            usage = getattr(resp, "usage_metadata", None)
            if usage and getattr(usage, "total_token_count", None):
                self.limiter.true_up(usage.total_token_count)

            try:
                text = self._extract_text(resp)
            except ContentBlocked:
                # Refusal is deterministic for a given prompt; the other two
                # attempts would buy three billed failures instead of one.
                raise
            except TeacherError as e:
                last_err = str(e)
                if attempt == self.max_retries:
                    break
                logger.warning("attempt %d/%d: %s; retrying", attempt, self.max_retries, last_err)
                continue

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                last_err = f"invalid JSON despite JSON mode: {e}"
                if attempt == self.max_retries:
                    break
                logger.warning("attempt %d/%d: %s; retrying", attempt, self.max_retries, last_err)
                continue

            if not isinstance(parsed, (dict, list)):
                raise TeacherError(f"expected a JSON object or array, got {type(parsed).__name__}")
            return parsed

        raise TeacherError(
            f"failed after {self.max_retries} attempts; last error: {last_err}",
            status=last_status,
        )
