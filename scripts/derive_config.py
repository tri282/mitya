"""Derive a serving config version from an existing one with field overrides.

Writes `config/serving/<hash>.json` and never moves `CURRENT`; promotion stays
`scripts/gate.py --promote` (A6). See `docs/v2/02-measurement-contract.md`.
"""

import argparse
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config_artifact import ServingConfig, load, read_pointer, save  # noqa: E402
from src.observability import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)


def parse_override(text: str) -> tuple[str, object]:
    """`field=value`; value is JSON when it parses, else the raw string."""
    field, sep, raw = text.partition("=")
    if not sep or not field:
        raise ValueError(f"override {text!r} is not field=value")
    try:
        return field, json.loads(raw)
    except json.JSONDecodeError:
        return field, raw


def load_seed(path: str) -> dict[str, object]:
    """A JSON object whose top-level keys are ServingConfig field overrides."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"seed {path!r} is not a JSON object")
    return data


def derive(base: ServingConfig, overrides: dict[str, object]) -> ServingConfig:
    unknown = set(overrides) - set(ServingConfig.model_fields)
    if unknown:
        raise ValueError(f"not ServingConfig fields: {sorted(unknown)}")
    # Rebuilt rather than model_copy(update=...), which skips validation.
    return ServingConfig(**{**base.model_dump(), **overrides})


def main(base_version: str | None, overrides: list[str], seed: str | None = None) -> str:
    configure_logging(logging.INFO)
    base_version = base_version or read_pointer()
    merged = {**(load_seed(seed) if seed else {}), **dict(parse_override(o) for o in overrides)}
    cfg = derive(load(base_version), merged)
    version = save(cfg)
    logger.info("derived %s from %s; CURRENT left alone", version, base_version)
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="version to derive from (default: the CURRENT pointer)")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="FIELD=VALUE", help="repeatable")
    parser.add_argument("--seed", help="JSON file of field overrides, applied before --set")
    args = parser.parse_args()
    if not args.overrides and not args.seed:
        parser.error("give --set and/or --seed")
    main(args.base, args.overrides, args.seed)
