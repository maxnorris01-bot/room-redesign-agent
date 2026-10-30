"""Admin commands for the deployed Shop Mode API: `python -m app.shop.admin <command>`.

    migrate                         create or update the database tables (safe to repeat)
    invite create --label NAME [--days N]
                                    make an invite; the code is printed once and never stored
    invite list                     label, created, last seen, spend today, revoked (never the code)
    invite revoke ID                end an invite and its logins at once
    caps show                       the spend caps in force and today's spend

It reads DATABASE_URL (and the cap settings) from the environment and never prints a secret.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.shop.auth import SqlAuth, utc_day
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
    invite = sub.add_parser("invite")
    isub = invite.add_subparsers(dest="action", required=True)
    create = isub.add_parser("create")
    create.add_argument("--label", required=True)
    create.add_argument("--days", type=int, default=None)
    isub.add_parser("list")
    revoke = isub.add_parser("revoke")
    revoke.add_argument("id")
    caps = sub.add_parser("caps")
    csub = caps.add_subparsers(dest="action", required=True)
    csub.add_parser("show")
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
        if args.command == "invite" and args.action == "create":
            if args.days is not None and args.days < 1:
                print("--days must be at least 1", file=sys.stderr)
                return EXIT_USAGE
            invite_id, code = auth.create_invite(args.label, args.days)
            print(f"invite {invite_id} created for {args.label!r}", file=write)
            print("code (shown once, not stored):", file=write)
            print(code, file=write)
            return 0
        if args.command == "invite" and args.action == "list":
            rows = auth.list_invites()
            if not rows:
                print("no invites", file=write)
            for r in rows:
                state = "revoked" if r["revoked"] else "active"
                print(
                    f"{r['id']}  {r['label']}  created {r['created']}  last seen {r['last_seen'] or 'never'}"
                    f"  spend today ${r['spend_today_usd']:.4f}  {state}",
                    file=write,
                )
            return 0
        if args.command == "invite" and args.action == "revoke":
            if auth.revoke_invite(args.id):
                print(f"invite {args.id} revoked", file=write)
                return 0
            print("no such invite", file=sys.stderr)
            return EXIT_USAGE
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
            print(f"per invite per day: ${settings.cap_invite_day_usd:.2f}", file=write)
            print(
                f"everyone per day: ${settings.cap_global_day_usd:.2f} (spent today ${spent:.4f})",
                file=write,
            )
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
