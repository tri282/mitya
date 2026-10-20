"""Jev `decide()` over OpenRouter's /systemone endpoint (v3 P0 B2, P-1 F18, F30).

A separate call class from generation; not wired into any request path (D4 is P3). The route pin
is asserted on the outgoing request. 1.14 nests the answer: {"suspect": {"type": "noul", "noul": p}}.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from src.config_artifact import AdapterProfile, ProviderRoute
from src.gateway.contract import DecideResult, Usage
from src.gateway.openai_compat import (
    TIMEOUT,
    USER_AGENT,
    ErrorClass,
    Fault,
    ProviderError,
    classify,
    decode_body,
    raise_fault,
    retry_after_s,
    route_payload,
)
from src.gateway.retry import TransportError
from src.pricing import UNPRICED, cost_usd

logger = logging.getLogger(__name__)


def noul_probability(answers: dict[str, Any], question_id: str) -> float:
    """The probability from a 1.14 noul answer. A bare number (1.13) or any other shape is
    refused, not coerced."""
    answer = answers.get(question_id)
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        raise ProviderError(ErrorClass.SHAPE, f"jev: answer {question_id!r} is not a nested noul")
    value = answer.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ProviderError(ErrorClass.SHAPE, f"jev: noul value for {question_id!r} is not a probability")
    return float(value)


class JevDecider:
    """Structural `Decider`. `route` is required (Jev is always pinned, F35); `rate_model` as in
    `OpenAICompatProvider` (None: list cost UNPRICED, billed from `usage.cost`)."""

    def __init__(
        self,
        model: str,
        profile: AdapterProfile,
        *,
        route: ProviderRoute,
        rate_model: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not profile.provider_routing:
            raise ValueError("jev is served through OpenRouter; the profile must take a routing pin")
        self.model = model
        self._profile = profile
        self._route = route
        self._rate_model = rate_model
        self._transport = transport

    def build_payload(self, state: str, questions: dict) -> dict[str, Any]:
        return {
            "model": self.model,
            "state": state,
            "questions": questions,
            "provider": route_payload(self._route),
        }

    async def decide(
        self, state: str, questions: dict, tenant: str, budget: float | None,
    ) -> DecideResult:
        logger.debug("jev.decide: model=%s tenant=%s budget=%s", self.model, tenant, budget)
        key = os.getenv(self._profile.key_env)
        if not key:
            raise ProviderError(ErrorClass.AUTH, f"jev: {self._profile.key_env} is not set")
        headers = {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
        url = self._profile.base_url + "/systemone"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                response = await client.post(url, json=self.build_payload(state, questions), headers=headers)
        except httpx.TimeoutException as exc:
            raise TransportError("jev: timed out", error_class=ErrorClass.SERVER.value) from exc
        except httpx.TransportError as exc:
            raise TransportError("jev: transport failure", error_class=ErrorClass.SERVER.value) from exc

        body = decode_body(response.content)
        fault = classify(response.status_code, body, self._profile.error_shape)
        if fault is not None:
            raise_fault(fault, "jev", retry_after_s(response.headers))
        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict):
            raise_fault(Fault(ErrorClass.SHAPE, response.status_code, None, "no answers"), "jev", None)

        usage = Usage(model=self.model)
        reported = body.get("usage")
        reported = reported if isinstance(reported, dict) else {}
        usage.prompt_tokens = reported.get("input_tokens")
        usage.completion_tokens = reported.get("output_tokens")
        cost = reported.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            usage.billed_usd = float(cost)
        usage.cost = (
            cost_usd(self._rate_model, usage.prompt_tokens, usage.completion_tokens)
            if self._rate_model else UNPRICED
        )
        # Observed, never hashed: the dated snapshot the alias resolved to (F30) and the upstream.
        if isinstance(body.get("model"), str):
            usage.resolved_model = body["model"]
        if isinstance(body.get("provider"), str):
            usage.upstream = body["provider"]
        return DecideResult(answers=answers, usage=usage)
