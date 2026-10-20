"""Shared helpers for the provider replay tests (v3 P0 B2): scrubbed fixtures, the committed
profiles and pins, and an in-memory transport so nothing touches the network.

Fixtures come from `python scripts/scrub_fixtures.py`; run it first.
"""

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, ".")

import httpx  # noqa: E402

from src.config_artifact import AdapterProfile, ProviderRoute  # noqa: E402
from src.gateway.openai_compat import CompatUsage, OpenAICompatProvider  # noqa: E402

FIXTURES = Path("test/fixtures/providers")
SEED_PATH = Path("config/gateway_seed.json")
TEST_KEY = "test-key-not-a-secret"
MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]

SEED = json.loads(SEED_PATH.read_text(encoding="utf-8"))


def profile(surface: str) -> AdapterProfile:
    return AdapterProfile(**SEED["adapter_profiles"][surface])


def route(model: str) -> ProviderRoute:
    return ProviderRoute(**SEED["provider_routes"][model])


def fixture(surface: str, name: str) -> dict:
    path = FIXTURES / surface / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run `python scripts/scrub_fixtures.py` first")
    return json.loads(path.read_text(encoding="utf-8"))


def transport(record: dict, seen: list | None = None) -> httpx.MockTransport:
    """Answers every request with the recorded status, headers and body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(
            record["status"], headers=record["headers"], content=record["body"].encode("utf-8"),
        )

    return httpx.MockTransport(handler)


def raw_transport(status: int, headers: dict, body: str, seen: list | None = None) -> httpx.MockTransport:
    return transport({"status": status, "headers": headers, "body": body}, seen)


def provider(
    surface: str, model: str, tr: httpx.MockTransport, *, rate_model: str | None = None,
) -> OpenAICompatProvider:
    prof = profile(surface)
    return OpenAICompatProvider(
        surface, model, prof,
        route=route(model) if prof.provider_routing else None,
        rate_model=rate_model, max_tokens=512, temperature=0.0, top_p=1.0, transport=tr,
    )


@dataclass
class Replay:
    text: str
    usage: CompatUsage
    error: BaseException | None


@contextmanager
def key_set(surface: str):
    """The surface's key env var set to a placeholder for the block, then restored."""
    env = profile(surface).key_env
    saved = os.environ.get(env)
    os.environ[env] = TEST_KEY
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = saved


def run(prov: OpenAICompatProvider, messages: list[dict] = MESSAGES) -> Replay:
    """Drain one generation; a raised fault is returned, not thrown."""

    async def go() -> Replay:
        stream = await prov.generate(messages, "default", "tenant-a", None)
        parts: list[str] = []
        error: BaseException | None = None
        try:
            async for chunk in stream.chunks:
                parts.append(chunk)
        except Exception as exc:  # the fault under test
            error = exc
        return Replay("".join(parts), stream.usage, error)

    with key_set(prov.surface):
        return asyncio.run(go())
