# test/test_limits_config.py
"""v3 P0 B1 verify (OI45): no limit, ceiling or rate primitive has a code default; the starting
values live in config/limits.json and a missing file or field refuses boot.

Plain-assert script. Offline, no network. Run from the repo root.
"""

import inspect
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")

from pydantic import ValidationError  # noqa: E402

from src import limits_config  # noqa: E402
from src.config import Config  # noqa: E402
from src.config_artifact import ConfigError  # noqa: E402
from src.gateway.guard import SpendRateBreaker, guarded_generate  # noqa: E402
from src.gateway.ledger import RequestLedger, SpendStore  # noqa: E402
from src.limits import RateLimiter, TenantConcurrency  # noqa: E402
from src.limits_config import LimitsConfig, SpendLimits, load_limits  # noqa: E402

COMMITTED = os.path.join("config", "limits.json")

# Known answers: the owner's starting values (schedule-p0 V rulings) and today's values for the
# knobs that moved out of code.
EXPECTED_FLAT = {
    "max_generation_attempts": 3,
    "chat_max_inflight_per_tenant": 2,
    "chat_max_inflight_total": 6,
    "chat_fairness_reserve": 2,
    "login_max_per_account": 5,
    "login_max_per_ip": 20,
    "pool_fairness_pressure": 0.8,
    "teacher_max_retries": 3,
    "teacher_timeout_ms": 120000,
    "teacher_max_output_tokens": 2048,
}
EXPECTED_SPEND = {
    "per_call_usd": 0.01,
    "per_request_usd": 0.02,
    "tenant_day_usd": 0.25,
    "tenant_day_tokens": 500_000,
    "global_minute_usd": 0.05,
    "global_day_usd": 1.0,
    "global_day_tokens": 2_000_000,
    "global_month_usd": 10.0,
}


def _committed() -> dict:
    with open(COMMITTED, encoding="utf-8") as f:
        return json.load(f)


def _write(data) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="mitya-limits-"), "limits.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    return path


def _refuses(path: str) -> ConfigError:
    try:
        load_limits(path)
    except ConfigError as exc:
        return exc
    raise AssertionError(f"{path} was accepted")


def test_committed_file_holds_the_starting_values() -> None:
    cfg = load_limits(COMMITTED)
    for name, value in EXPECTED_FLAT.items():
        assert getattr(cfg, name) == value, name
    for name, value in EXPECTED_SPEND.items():
        assert getattr(cfg.spend, name) == value, name
    assert set(EXPECTED_FLAT) | {"spend"} == set(LimitsConfig.model_fields)
    assert set(EXPECTED_SPEND) == set(SpendLimits.model_fields)


def test_every_field_raises_without_a_value() -> None:
    """Each inventoried knob and each spend layer: delete the value, the load refuses."""
    for name in EXPECTED_FLAT:
        data = _committed()
        del data[name]
        assert name in str(_refuses(_write(data))), name
    for name in EXPECTED_SPEND:
        data = _committed()
        del data["spend"][name]
        assert name in str(_refuses(_write(data))), name
    data = _committed()
    del data["spend"]
    _refuses(_write(data))


def test_unknown_field_and_bad_values_are_refused() -> None:
    data = _committed()
    data["spend_rate_default"] = 0.1
    _refuses(_write(data))
    data = _committed()
    data["spend"]["global_day_usd"] = 0
    _refuses(_write(data))
    data = _committed()
    data["chat_max_inflight_total"] = "six"
    _refuses(_write(data))
    _refuses(_write("{not json"))


def test_boot_refuses_with_a_missing_limits_file() -> None:
    saved = limits_config.LIMITS_PATH
    limits_config.LIMITS_PATH = os.path.join(tempfile.mkdtemp(), "absent.json")
    limits_config.reset_limits()
    try:
        for read in (limits_config.active_limits, lambda: Config.CHAT_MAX_INFLIGHT_TOTAL,
                     lambda: Config.MAX_GENERATION_ATTEMPTS, lambda: Config.LOGIN_MAX_PER_IP):
            try:
                read()
            except ConfigError as exc:
                assert "limits config" in str(exc)
            else:
                raise AssertionError("a limit resolved with no limits file")
    finally:
        limits_config.LIMITS_PATH = saved
        limits_config.reset_limits()


def test_lifespan_checks_the_limits_before_anything_else_loads() -> None:
    import src.api.app as app_mod

    source = inspect.getsource(app_mod.lifespan)
    assert "active_limits()" in source
    assert source.index("active_limits()") < source.index("TextEmbedding(")


def test_config_facade_resolves_through_the_file_with_no_code_default() -> None:
    for name, value in EXPECTED_FLAT.items():
        assert getattr(Config, name.upper()) == value, name
    for removed in ("SPEND_RATE_MAX_USD_PER_MINUTE",):
        try:
            getattr(Config, removed)
        except AttributeError:
            pass
        else:
            raise AssertionError(f"{removed} still resolves")
    with open(os.path.join("src", "config.py"), encoding="utf-8") as f:
        config_source = f.read()
    for name in EXPECTED_FLAT:
        assert f'getenv("{name.upper()}"' not in config_source, f"{name.upper()} has a code default again"


def _no_defaults(fn, names) -> None:
    params = inspect.signature(fn).parameters
    for name in names:
        assert params[name].default is inspect.Parameter.empty, f"{fn}: {name} has a default"


def test_limit_primitives_take_their_value_as_a_required_argument() -> None:
    _no_defaults(RateLimiter.__init__, ("rpm", "tpm", "rpd"))
    _no_defaults(TenantConcurrency.__init__, ("per_tenant", "total", "reserve"))
    _no_defaults(SpendRateBreaker.__init__, ("max_usd_per_minute", "fairness_pressure"))
    _no_defaults(SpendStore.__init__, ("limits",))
    _no_defaults(RequestLedger.__init__, ("limits", "store"))
    _no_defaults(guarded_generate, (
        "limiter", "breaker", "ledger", "estimated_cost_usd", "max_input_tokens",
        "max_history_messages", "max_output_tokens",
    ))
    assert "budget" not in inspect.signature(guarded_generate).parameters
    for build in (lambda: SpendRateBreaker(0.05), lambda: TenantConcurrency(2, 6),
                  lambda: RateLimiter(rpm=1, tpm=1)):
        try:
            build()
        except TypeError:
            pass
        else:
            raise AssertionError("a limit primitive was constructed without a value")
    for model in (LimitsConfig, SpendLimits):
        try:
            model()
        except ValidationError:
            pass
        else:
            raise AssertionError(f"{model.__name__}() built with no values")


def test_no_budget_none_on_the_request_path() -> None:
    import src.rag.generate as gen

    assert "budget=None" not in inspect.getsource(gen.open_guarded_stream)
    assert "ledger=current_ledger()" in inspect.getsource(gen.open_guarded_stream)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
