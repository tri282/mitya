"""Generic OpenAI-dialect streaming adapter, one `AdapterProfile` per surface (v3 P0 B2, I-R53 amended).

Dialect facts are the recorded ones in P-1 findings F4, F11, F23-F29 and VIII.1. Error
classification never trusts the HTTP status alone. Prior art: ../christina/src/providers/openai_compat.py.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NoReturn

import httpx

from src.config import Config
from src.config_artifact import AdapterProfile, ProviderRoute
from src.gateway.contract import GenerationStream, Usage
from src.gateway.retry import TransportError
from src.pricing import UNPRICED, cost_usd

logger = logging.getLogger(__name__)

# Cloudflare-fronted surfaces 403 urllib's default agent (christina P-1-F22); explicit on every call.
USER_AGENT = "mitya-gateway/0.1"
TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0)
_ERROR_FORMS = ("object", "string", "detail")


class ErrorClass(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    CLIENT = "client"
    EMPTY = "empty"
    SHAPE = "shape"


RETRYABLE = frozenset({ErrorClass.RATE_LIMIT, ErrorClass.SERVER, ErrorClass.EMPTY})


class ProviderError(RuntimeError):
    """A fault that retrying will not fix (auth, quota, client, shape)."""

    def __init__(self, error_class: ErrorClass, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.status = status


@dataclass(frozen=True)
class Fault:
    error_class: ErrorClass
    # The HTTP status, or for a 200 carrying an error the body's own code (None when it has none).
    status: int | None
    # Which error shape the body used; None when the body carried no error object.
    form: str | None
    message: str


UNDECODABLE = object()


def decode_body(raw: bytes) -> Any:
    """Decoded JSON; None for an empty body or JSON `null` (Purdue rate limit, F11);
    `UNDECODABLE` for anything else."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return UNDECODABLE


def _class_for_status(status: int) -> ErrorClass:
    if status in (401, 403):
        return ErrorClass.AUTH
    if status == 402:
        return ErrorClass.QUOTA
    if status == 429:
        return ErrorClass.RATE_LIMIT
    if status == 408 or status >= 500:
        return ErrorClass.SERVER
    return ErrorClass.CLIENT


def _find_error(body: Any, declared: str) -> tuple[str, str, int | None, bool] | None:
    """(form, message, in-band code, key-limit flag) of the first error shape found, the
    declared one first."""
    if not isinstance(body, dict):
        return None
    err, detail = body.get("error"), body.get("detail")
    found: dict[str, tuple[str, int | None, bool]] = {}
    if isinstance(err, dict):
        code = err.get("code")
        code = code if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 600 else None
        meta = err.get("metadata")
        found["object"] = (str(err.get("message") or ""), code, isinstance(meta, dict) and "limit_source" in meta)
    elif isinstance(err, str):
        found["string"] = (err, None, False)
    if isinstance(detail, str):
        found["detail"] = (detail, None, False)
    for form in (declared, *(f for f in _ERROR_FORMS if f != declared)):
        if form in found:
            message, code, key_limit = found[form]
            return form, message[:200], code, key_limit
    return None


def classify(status: int, body: Any, declared: str) -> Fault | None:
    """The fault a response carries, or None. An error object wins over a 200 status; a JSON
    `null` body is a fault even at 200 (P-1 F4, F11)."""
    found = _find_error(body, declared)
    if found is not None:
        form, message, code, key_limit = found
        effective = status if status >= 400 else code
        if key_limit:
            error_class = ErrorClass.QUOTA
        else:
            error_class = _class_for_status(effective) if effective is not None else ErrorClass.SERVER
        return Fault(error_class, effective, form, message)
    if status >= 400:
        return Fault(_class_for_status(status), status, None, "")
    if body is UNDECODABLE:
        return Fault(ErrorClass.SHAPE, status, None, "")
    if body is None:
        return Fault(ErrorClass.EMPTY, status, None, "")
    return None


def retry_after_s(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except ValueError:
        logger.warning("gateway: unparseable retry-after %r", str(raw)[:20])
        return None


def raise_fault(fault: Fault, surface: str, retry_after: float | None) -> NoReturn:
    """Retryable classes become `TransportError` (the existing retry loop's vocabulary); the
    rest `ProviderError`. Provider text stays out of the message, which can reach a client."""
    text = f"{surface}: {fault.error_class.value}" + (f" (HTTP {fault.status})" if fault.status else "")
    logger.warning("gateway: %s fault, form=%s", text, fault.form)
    logger.debug("gateway: %s provider message: %r", surface, fault.message[:80])
    if fault.error_class in RETRYABLE:
        raise TransportError(text, retry_after=retry_after, error_class=fault.error_class.value)
    raise ProviderError(fault.error_class, text, fault.status)


def route_payload(route: ProviderRoute) -> dict[str, Any]:
    """The OpenRouter `provider` object for a pin; `only` is sent when non-empty."""
    out: dict[str, Any] = {
        "order": list(route.order), "allow_fallbacks": route.allow_fallbacks, "zdr": route.zdr,
    }
    if route.only:
        out["only"] = list(route.only)
    return out


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass
class CompatUsage(Usage):
    """`Usage` plus what the dialect lets us observe. `reasoning_chars` counts streamed reasoning
    text; set against `reasoning_tokens` it shows whether a surface reports reasoning (F27)."""

    reasoning_tokens: int | None = None
    reasoning_chars: int = 0
    ratelimit: dict[str, str] = field(default_factory=dict)


@dataclass
class _State:
    last_usage: dict | None = None
    usage_chunks: int = 0
    finished: bool = False
    saw_data: bool = False


class OpenAICompatProvider:
    """Structural `Provider`. `route` and `rate_model` are required: a routing surface has no
    unpinned send, and `rate_model` names the `Config.MODEL_RATES` row that prices this binding
    (None: UNPRICED, because the rate table is keyed by model id and not by surface)."""

    def __init__(
        self,
        surface: str,
        model: str,
        profile: AdapterProfile,
        *,
        route: ProviderRoute | None,
        rate_model: str | None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if profile.provider_routing and route is None:
            raise ValueError(f"{surface}: {model} has no route pin; an OpenRouter send is always pinned")
        if route is not None and not profile.provider_routing:
            raise ValueError(f"{surface} does not take a routing pin")
        self.surface = surface
        self.model = model
        self._profile = profile
        self._route = route
        self._rate_model = rate_model
        self._max_tokens = max_tokens if max_tokens is not None else Config.MAX_TOKENS
        self._temperature = temperature if temperature is not None else Config.GEN_TEMPERATURE
        self._top_p = top_p if top_p is not None else Config.GEN_TOP_P
        self._transport = transport

    def build_payload(self, messages: list[dict]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "stream": True,
        }
        if self._profile.request_usage:
            payload["stream_options"] = {"include_usage": True}
        if self._route is not None:
            payload["provider"] = route_payload(self._route)
        return payload

    async def generate(
        self, messages: list[dict], tier: str, tenant: str, budget: float | None,
    ) -> GenerationStream:
        logger.debug("%s.generate: model=%s tier=%s tenant=%s", self.surface, self.model, tier, tenant)
        usage = CompatUsage(model=self.model)
        return GenerationStream(chunks=self._chunks(self.build_payload(messages), usage), usage=usage)

    def _finalize(self, usage: CompatUsage, st: _State) -> None:
        reported = st.last_usage
        observed_absent = st.usage_chunks == 0
        if observed_absent != (self._profile.usage == "absent"):
            logger.warning(
                "%s adapter: profile says usage=%s but %d usage chunks arrived",
                self.surface, self._profile.usage, st.usage_chunks,
            )
        if reported is None:
            return
        usage.prompt_tokens = reported.get("prompt_tokens")
        usage.completion_tokens = reported.get("completion_tokens")
        details = reported.get("completion_tokens_details")
        usage.reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if self._profile.cost_field:
            usage.billed_usd = _number(reported.get(self._profile.cost_field))
        usage.cost = (
            cost_usd(self._rate_model, usage.prompt_tokens, usage.completion_tokens)
            if self._rate_model else UNPRICED
        )

    def _fail(self, fault: Fault, usage: CompatUsage, st: _State, retry_after: float | None) -> NoReturn:
        self._finalize(usage, st)
        raise_fault(fault, self.surface, retry_after)

    def _on_data(self, data: str, usage: CompatUsage, st: _State, retry_after: float | None) -> str | None:
        """One SSE `data:` payload. Returns answer text to yield, or None."""
        st.saw_data = True
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("%s adapter: unparseable SSE chunk (%d chars)", self.surface, len(data))
            return None
        if chunk is None:
            self._fail(Fault(ErrorClass.EMPTY, 200, None, ""), usage, st, retry_after)
        if not isinstance(chunk, dict):
            return None
        fault = classify(200, chunk, self._profile.error_shape)
        if fault is not None:
            usage.finish_reason = "error"
            self._fail(fault, usage, st, retry_after)
        upstream, resolved = chunk.get("provider"), chunk.get("model")
        if isinstance(upstream, str):
            usage.upstream = upstream
        if isinstance(resolved, str):
            usage.resolved_model = resolved
        reported = chunk.get("usage")
        if isinstance(reported, dict):
            st.last_usage = reported
            st.usage_chunks += 1
        choices = chunk.get("choices")
        if not choices or not isinstance(choices[0], dict):
            return None
        choice = choices[0]
        delta = choice.get("delta") or {}
        # A surface may repeat one reasoning text under two field names; count it once.
        usage.reasoning_chars += max(
            (len(t) for name in self._profile.reasoning_fields if isinstance(t := delta.get(name), str)),
            default=0,
        )
        if choice.get("finish_reason"):
            usage.finish_reason = choice["finish_reason"]
            st.finished = True
        if choice.get("native_finish_reason"):
            usage.native_finish_reason = choice["native_finish_reason"]
        content = delta.get("content")
        return content if isinstance(content, str) and content else None

    async def _chunks(self, payload: dict[str, Any], usage: CompatUsage) -> AsyncIterator[str]:
        key = os.getenv(self._profile.key_env)
        if not key:
            raise ProviderError(ErrorClass.AUTH, f"{self.surface}: {self._profile.key_env} is not set")
        headers = {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
        url = self._profile.base_url + "/chat/completions"
        st = _State()
        done = False
        other: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    retry_after = retry_after_s(response.headers)
                    usage.ratelimit = {
                        k.lower(): v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit-")
                    }
                    if response.status_code != 200:
                        body = decode_body(await response.aread())
                        # Every non-200 classifies: a status >= 400 always yields a fault.
                        fault = classify(response.status_code, body, self._profile.error_shape) or Fault(
                            _class_for_status(response.status_code), response.status_code, None, "",
                        )
                        raise_fault(fault, self.surface, retry_after)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            if line.strip() and not line.startswith(":") and len(other) < 256:
                                other.append(line)
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            done = True
                            break
                        text = self._on_data(data, usage, st, retry_after)
                        if text:
                            yield text
        except httpx.TimeoutException as exc:
            raise TransportError(f"{self.surface}: timed out", error_class=ErrorClass.SERVER.value) from exc
        except httpx.TransportError as exc:
            raise TransportError(f"{self.surface}: transport failure", error_class=ErrorClass.SERVER.value) from exc

        if not st.saw_data:
            # A 200 with no SSE events: a JSON error, a `null` body, or nothing (F4, F11).
            body = decode_body("\n".join(other).encode("utf-8"))
            self._fail(
                classify(200, body, self._profile.error_shape) or Fault(ErrorClass.SHAPE, 200, None, ""),
                usage, st, None,
            )
        self._finalize(usage, st)
        if not done and not st.finished:
            # No [DONE] and no finish reason: the stream was cut without an error object (F7).
            usage.finish_reason = "error"
            raise_fault(Fault(ErrorClass.SERVER, 200, None, "stream ended without a finish"), self.surface, None)
