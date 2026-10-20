"""Loader and model for config/limits.json, the one place limit and ceiling values live (OI45, OI8).

No code default exists for any field: a missing file or field refuses boot.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config_artifact import CONFIG_DIR, ConfigError

logger = logging.getLogger(__name__)

LIMITS_PATH = os.path.join(CONFIG_DIR, "limits.json")

_PosInt = Annotated[int, Field(gt=0)]
_PosFloat = Annotated[float, Field(gt=0)]
_NonNegInt = Annotated[int, Field(ge=0)]


class SpendLimits(BaseModel):
    """The six spend layers plus the unpriced per-call token cap. Reset boundaries for the day and month sums are UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    per_call_usd: _PosFloat
    per_request_usd: _PosFloat
    tenant_day_usd: _PosFloat
    tenant_day_tokens: _PosInt
    global_minute_usd: _PosFloat
    global_day_usd: _PosFloat
    global_day_tokens: _PosInt
    global_month_usd: _PosFloat
    # Per-call cap, in tokens, for a call with no list price. The USD layers do not bind such a
    # call; the token layers and this cap do (schedule-p0 V rulings, 2026-10-20).
    unpriced_call_tokens: _PosInt


class LimitsConfig(BaseModel):
    """Field names are the lowercase of their `Config` counterparts; the facade in
    src/config.py resolves `Config.<NAME>` through this model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_generation_attempts: _PosInt
    chat_max_inflight_per_tenant: _PosInt
    chat_max_inflight_total: _PosInt
    chat_fairness_reserve: _NonNegInt
    login_max_per_account: _PosInt
    login_max_per_ip: _PosInt
    pool_fairness_pressure: _PosFloat
    teacher_max_retries: _NonNegInt
    teacher_timeout_ms: _PosInt
    teacher_max_output_tokens: _PosInt
    teacher_rpm: _PosInt
    teacher_tpm: _PosInt
    teacher_rpd: _PosInt
    login_window_seconds: _PosFloat
    chat_fairness_window_seconds: _PosFloat
    chat_fairness_tolerance: _PosFloat
    spend: SpendLimits


def load_limits(path: str | None = None) -> LimitsConfig:
    """Fail-closed: a missing, unreadable or incomplete file raises `ConfigError`."""
    path = path or LIMITS_PATH
    if not os.path.exists(path):
        raise ConfigError(f"no limits config at {path} -- refusing to start (OI45).")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"limits config {path!r} is unreadable: {exc}") from exc
    try:
        return LimitsConfig(**data)
    except (ValidationError, TypeError) as exc:
        raise ConfigError(f"limits config {path!r} failed validation: {exc}") from exc


_active: LimitsConfig | None = None


def active_limits() -> LimitsConfig:
    global _active
    if _active is None:
        _active = load_limits()
        logger.info("active limits loaded from %s", LIMITS_PATH)
    return _active


def reset_limits() -> None:
    global _active
    _active = None
