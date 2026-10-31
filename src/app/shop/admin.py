"""Admin commands for the deployed Shop Mode API: `python -m app.shop.admin <command>`.

    migrate       create or update the database tables (safe to repeat)
    caps show     the limits in force, today's spend and the visitors started today
    purge         delete visitors, logins and sessions unused for APP_VISITOR_RETENTION_DAYS (30)
                  and usage counters older than 40 days (also runs at start and every 6 hours)

It reads DATABASE_URL (and the limit settings) from the environment and never prints a secret or
a client address.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.shop.auth import SqlAuth, purge, utc_day
from app.shop.dbstore import DatabaseError, Tx, migrate, open_database
from app.shop.deploy import from_env

EXIT_USAGE = 2


def run(
    argv: list[str] | None = None, environ: Mapping[str, str] | None = None, out: Any = None
) -> int:
    env = os.environ if environ is None else environ
    write = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m app.shop.admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    caps = sub.add_parser("caps")
    csub = caps.add_subparsers(dest="action", required=True)
    csub.add_parser("show")
    sub.add_parser("purge")
    args = parser.parse_args(argv)

    settings = from_env(env)
    url = env.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return EXIT_USAGE
    try:
        db = open_database(url)
        if args.command == "migrate":
            print(f"database is at schema version {migrate(db)}", file=write)
            return 0
        auth = SqlAuth(db, settings)
        if args.command == "purge":
            gone = purge(db, datetime.now(UTC), settings.retention_days)
            print("purged: " + ", ".join(f"{k} {v}" for k, v in gone.items()), file=write)
            return 0
        if args.command == "caps":
            today = utc_day(datetime.now(UTC))

            def read(tx: Tx) -> float:
                rows = tx.execute(
                    "SELECT usd FROM usage WHERE scope = 'global_day' AND key = '*' AND day = ?",
                    (today,),
                )
                return float(rows[0][0]) if rows else 0.0

            spent = db.transaction(read)
            print(f"per session: ${settings.cap_session_usd:.2f}, {settings.cap_searches} searches, "
                  f"{settings.cap_shop_agains} Shop Agains", file=write)  # fmt: skip
            print(f"per visitor address per day: ${settings.cap_ip_day_usd:.2f}", file=write)
            print(
                f"everyone per day: ${settings.cap_global_day_usd:.2f} (spent today ${spent:.4f})",
                file=write,
            )
            print(
                f"visitors started today: {auth.visitors_today()} of {settings.max_visitors_day} "
                f"(one address: {settings.max_visitors_per_ip_day} a day, "
                f"{settings.starts_per_ip_minute} a minute)",
                file=write,
            )
            print(f"visitor data kept {settings.retention_days} days", file=write)
            print(
                f"kill switch (APP_SHOP_DISABLED): {'ON' if settings.disabled else 'off'}",
                file=write,
            )
            return 0
    except DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return EXIT_USAGE


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
