"""Start Shop Mode: locally as before, or as the deployed API (`APP_DEPLOY_MODE=1`).

    python -m app.shop.serve            # the container's command

Local use goes through `scripts/shop.py` (`make shop`); this module is the deployment entry point.
It refuses to start when deployment mode is on and a required setting is missing, and never prints a
secret.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.config import Config, load_env
from app.shop.auth import SqlAuth
from app.shop.caps import CapGuard, Meter
from app.shop.dbstore import SqlSessionStore, migrate, open_store
from app.shop.deploy import DeploySettings, from_env
from app.shop.providers import build_shop_providers
from app.shop.server import ShopApp, make_server
from app.shop.service import ShopService
from app.shop.store import DEFAULT_ROOT


def build_app(
    settings: DeploySettings, config: Config, data_dir: Path = DEFAULT_ROOT
) -> tuple[ShopApp, Any]:
    """The app for `settings` (the database store, login and caps in deployment mode)."""
    store = open_store(settings.database_url if settings.deploy else "", data_dir)
    search, lookup = build_shop_providers(config)
    service = ShopService(config, store, search, lookup)
    if not settings.deploy:
        return ShopApp(service), store
    assert isinstance(store, SqlSessionStore)
    guard = CapGuard(store.db, settings)
    store.after_save = Meter()
    service.guard = guard
    service.prep.guard = guard
    auth = SqlAuth(store.db, settings)
    return ShopApp(service, settings.port, settings, auth, store), store


def main(environ: Mapping[str, str] | None = None) -> int:
    load_env()
    env = os.environ if environ is None else environ
    settings = from_env(env)
    problems = settings.problems()
    if problems:
        for line in problems:
            print(f"ERROR: {line}", file=sys.stderr)
        return 2
    config = Config.from_env()
    if settings.deploy:
        missing = [
            k
            for k in ("ANTHROPIC_API_KEY", "SERPER_API_KEY", "SERPAPI_API_KEY")
            if config.llm_mode == "live" and not env.get(k)
        ]
        if missing:
            print(
                f"ERROR: live mode needs these variables set: {', '.join(missing)}", file=sys.stderr
            )
            return 2
    app, store = build_app(settings, config)
    if settings.deploy:
        migrate(store.db)  # idempotent: the machine can start on a fresh database
    server = make_server(app, settings.port, settings.host)
    print(f"Shop Mode API listening on {settings.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
