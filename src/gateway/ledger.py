"""Request-scoped ledger and the six spend layers (OI8, OI45); persistence in src/db.py's `spend` table.

Every priced call is admitted against all six layers before it is sent and settled after it returns.
"""

from __future__ import annotations

import contextvars
import logging
import math
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from src.db import SPEND_SCHEMA, connect
from src.gateway.contract import Usage
from src.limits import LimitError
from src.limits_config import SpendLimits, active_limits
from src.pricing import FREE, UNPRICED, Cost, total_usd

logger = logging.getLogger(__name__)


class SpendLayer(StrEnum):
    PER_CALL = "per_call"
    PER_REQUEST = "per_request"
    TENANT_DAY = "tenant_day"
    GLOBAL_MINUTE = "global_minute"
    GLOBAL_DAY = "global_day"
    GLOBAL_MONTH = "global_month"


class BudgetExceeded(LimitError):
    """A spend layer refused. `retry_after_s` is None when the same request would refuse again."""

    def __init__(self, layer: SpendLayer, retry_after_s: int | None) -> None:
        self.layer = layer
        self.retry_after_s = retry_after_s
        if retry_after_s is None:
            message = f"request exceeds its budget ({layer.value})"
        else:
            message = f"spend limit reached ({layer.value}); try again in {retry_after_s}s"
        super().__init__(message, status=429)

    def refusal(self) -> dict:
        return {"layer": self.layer.value, "retry_after_s": self.retry_after_s}


class LedgerMissing(RuntimeError):
    """A priced call was attempted outside a request ledger. No call runs without a budget."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def seconds_to_next_utc_midnight(now: datetime) -> int:
    now = now.astimezone(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((nxt - now).total_seconds()))


def seconds_to_next_utc_month(now: datetime) -> int:
    now = now.astimezone(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    nxt = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((nxt - now).total_seconds()))


@dataclass(frozen=True)
class Hold:
    """What `SpendStore.reserve` wrote, with the period keys it wrote them under."""

    tenant: str
    day: str
    month: str
    usd: float
    tokens: int


class SpendStore:
    """Tenant-day, global-day and global-month sums, persisted in SQLite (UTC boundaries)."""

    def __init__(
        self,
        limits: SpendLimits,
        db_path: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._limits = limits
        self._db_path = db_path
        self._clock = clock

    @contextmanager
    def _conn(self):
        with connect(self._db_path) as conn:
            conn.execute(SPEND_SCHEMA)
            yield conn

    @staticmethod
    def _get(conn, scope: str, period: str) -> tuple[float, int]:
        row = conn.execute(
            "SELECT usd, tokens FROM spend WHERE scope = ? AND period = ?", (scope, period)
        ).fetchone()
        return (row["usd"], row["tokens"]) if row else (0.0, 0)

    @staticmethod
    def _add(conn, scope: str, period: str, usd: float, tokens: int) -> None:
        conn.execute(
            "INSERT INTO spend (scope, period, usd, tokens) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (scope, period) DO UPDATE SET usd = usd + excluded.usd, "
            "tokens = tokens + excluded.tokens",
            (scope, period, usd, tokens),
        )

    def reserve(self, tenant: str, usd: float, tokens: int) -> Hold:
        """Check the three persisted layers and add the estimate in one transaction."""
        lim = self._limits
        now = self._clock()
        day, month = f"day:{now:%Y-%m-%d}", f"month:{now:%Y-%m}"
        t_scope = f"tenant:{tenant}"
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            t_usd, t_tok = self._get(conn, t_scope, day)
            gd_usd, gd_tok = self._get(conn, "global", day)
            gm_usd, _ = self._get(conn, "global", month)
            if t_usd + usd > lim.tenant_day_usd or t_tok + tokens > lim.tenant_day_tokens:
                raise BudgetExceeded(SpendLayer.TENANT_DAY, seconds_to_next_utc_midnight(now))
            if gd_usd + usd > lim.global_day_usd or gd_tok + tokens > lim.global_day_tokens:
                raise BudgetExceeded(SpendLayer.GLOBAL_DAY, seconds_to_next_utc_midnight(now))
            if gm_usd + usd > lim.global_month_usd:
                raise BudgetExceeded(SpendLayer.GLOBAL_MONTH, seconds_to_next_utc_month(now))
            self._add(conn, t_scope, day, usd, tokens)
            self._add(conn, "global", day, usd, tokens)
            self._add(conn, "global", month, usd, tokens)
        return Hold(tenant=tenant, day=day, month=month, usd=usd, tokens=tokens)

    def settle(self, hold: Hold, actual_usd: float, actual_tokens: int) -> None:
        """Replace the reserved estimate with the actual; sums never go below zero."""
        d_usd, d_tok = actual_usd - hold.usd, actual_tokens - hold.tokens
        with self._conn() as conn:
            for scope, period in (
                (f"tenant:{hold.tenant}", hold.day), ("global", hold.day), ("global", hold.month),
            ):
                conn.execute(
                    "UPDATE spend SET usd = MAX(usd + ?, 0), tokens = MAX(tokens + ?, 0) "
                    "WHERE scope = ? AND period = ?",
                    (d_usd, d_tok, scope, period),
                )

    def release(self, hold: Hold) -> None:
        self.settle(hold, 0.0, 0)

    def totals(self, scope: str, period: str) -> tuple[float, int]:
        with self._conn() as conn:
            return self._get(conn, scope, period)


@dataclass
class LedgerEntry:
    model: str | None
    estimate_usd: float
    cost: Cost | None = None
    billed_usd: float | None = None
    tokens: int | None = None
    outcome: str = "open"  # open | ok | failed


@dataclass(frozen=True)
class LedgerTotals:
    list_cost: Cost
    billed: Cost
    calls: int
    failed_calls: int


@dataclass
class Reservation:
    entry: LedgerEntry
    hold: Hold
    estimate_tokens: int


class RequestLedger:
    """Sums every priced call of one request. List and billed are kept apart; an unknown
    cost stays UNPRICED, never 0 (N-EV10)."""

    def __init__(self, limits: SpendLimits, store: SpendStore) -> None:
        self._limits = limits
        self._store = store
        self.entries: list[LedgerEntry] = []

    def _committed_usd(self) -> float:
        return sum(
            e.cost.usd if e.cost is not None and e.cost.usd is not None else e.estimate_usd
            for e in self.entries if e.outcome != "failed"
        )

    def remaining_usd(self) -> float:
        left = min(self._limits.per_call_usd, self._limits.per_request_usd - self._committed_usd())
        return max(0.0, left)

    def check(self, estimate_usd: float) -> None:
        if estimate_usd > self._limits.per_call_usd:
            raise BudgetExceeded(SpendLayer.PER_CALL, None)
        if self._committed_usd() + estimate_usd > self._limits.per_request_usd:
            raise BudgetExceeded(SpendLayer.PER_REQUEST, None)

    def reserve(self, tenant: str, model: str | None, estimate_usd: float, estimate_tokens: int) -> Reservation:
        self.check(estimate_usd)
        hold = self._store.reserve(tenant, estimate_usd, estimate_tokens)
        entry = LedgerEntry(model=model, estimate_usd=estimate_usd)
        self.entries.append(entry)
        return Reservation(entry=entry, hold=hold, estimate_tokens=estimate_tokens)

    def settle(self, res: Reservation, usage: Usage) -> None:
        entry = res.entry
        entry.cost = usage.cost
        entry.billed_usd = usage.billed_usd
        if usage.prompt_tokens is not None and usage.completion_tokens is not None:
            entry.tokens = usage.prompt_tokens + usage.completion_tokens
        entry.outcome = "ok"
        # An unknown actual is charged at its estimate, a ceiling, never at 0.
        actual_usd = usage.cost.usd if usage.cost.usd is not None else res.entry.estimate_usd
        actual_tokens = entry.tokens if entry.tokens is not None else res.estimate_tokens
        self._store.settle(res.hold, actual_usd, actual_tokens)

    def fail(self, res: Reservation) -> None:
        """The call produced nothing: the reservation is released."""
        res.entry.outcome = "failed"
        self._store.release(res.hold)

    def totals(self) -> LedgerTotals:
        live = [e for e in self.entries if e.outcome != "failed"]
        list_costs = [e.cost if e.cost is not None else UNPRICED for e in live]
        billed_costs = []
        for e, c in zip(live, list_costs):
            if e.billed_usd is not None:
                billed_costs.append(Cost(usd=e.billed_usd, metered=True))
            elif c.usd is not None and not c.metered:
                billed_costs.append(FREE)
            else:
                billed_costs.append(UNPRICED)
        return LedgerTotals(
            list_cost=total_usd(list_costs),
            billed=total_usd(billed_costs),
            calls=len(live),
            failed_calls=len(self.entries) - len(live),
        )


_current: contextvars.ContextVar[RequestLedger | None] = contextvars.ContextVar(
    "request_ledger", default=None
)


def current_ledger() -> RequestLedger:
    ledger = _current.get()
    if ledger is None:
        raise LedgerMissing("priced call outside a request ledger -- open one with request_ledger()")
    return ledger


def set_current_ledger(ledger: RequestLedger) -> None:
    _current.set(ledger)


_store: SpendStore | None = None


def spend_store() -> SpendStore:
    global _store
    if _store is None:
        _store = SpendStore(active_limits().spend)
    return _store


def reset_spend_store() -> None:
    global _store
    _store = None


def new_request_ledger() -> RequestLedger:
    return RequestLedger(active_limits().spend, spend_store())


@contextmanager
def request_ledger():
    """Scope for scripts and tests: one ledger bound for the duration."""
    ledger = new_request_ledger()
    token = _current.set(ledger)
    try:
        yield ledger
    finally:
        _current.reset(token)
