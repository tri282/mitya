#!/usr/bin/env python3
"""Seed one account. The ONLY account-creation path (v2 P5 step 2).

Registration is CLOSED (decisions-p5.md): D7 permits closed or invite-only,
and closed is the smaller surface -- no invite tokens to mint, expire or leak,
and no `POST /auth/register` for anyone to find. The cost is that adding a
tenant means shell access to the box, which for a single-operator deployment
(D3, D8) is not a cost.

    python scripts/seed_account.py alice
    python scripts/seed_account.py alice --password-env MITYA_SEED_PASSWORD

With no `--password-env`, the password is read interactively via `getpass`,
which keeps it out of shell history, out of `ps`, and off the screen. The
env-var form exists for the harness's own accounts, which are created by a
script rather than a person.

The account id IS the tenant id and reaches log lines and span attributes
verbatim, so it is shape-checked before insert (`src.observability.is_valid_tenant`).
"""

import argparse
import getpass
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.auth import create_account  # noqa: E402
from src.config import Config  # noqa: E402
from src.db import init_schema  # noqa: E402
from src.observability import configure_logging  # noqa: E402

logger = logging.getLogger("seed_account")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="account id; also the tenant id")
    parser.add_argument(
        "--password-env",
        help="read the password from this environment variable instead of prompting",
    )
    args = parser.parse_args()

    configure_logging(logging.INFO)

    if args.password_env:
        password = os.environ.get(args.password_env)
        if not password:
            logger.error("seed: %s is unset or empty", args.password_env)
            return 2
    else:
        password = getpass.getpass("password: ")
        if password != getpass.getpass("confirm: "):
            logger.error("seed: passwords do not match")
            return 2

    # The script may be the first thing to touch the database, so it creates the
    # schema itself rather than assuming the server has started once.
    init_schema()
    try:
        create_account(args.username, password)
    except sqlite3.IntegrityError:
        # Deliberately not a password reset. Changing a password is a different
        # operation with different consequences (every live session for that
        # account should die with it) and nobody has asked for one yet.
        logger.error("seed: account %r already exists", args.username)
        return 1
    except ValueError as exc:
        logger.error("seed: %s", exc)
        return 2

    logger.info("seed: account %r created in %s", args.username, Config.DB_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
