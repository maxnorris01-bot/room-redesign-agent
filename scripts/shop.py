"""Shop Mode: a curated product search page on http://127.0.0.1 (ADR 0012).

    make shop        # mock mode: fixture results, no network, no key
    make shop-live   # live mode: real Anthropic, Serper and SerpApi calls (costs money)

Type a request, rate the results, open one. Sessions are saved under shop-data/ (gitignored).
The server listens on 127.0.0.1 only. Stop it with Ctrl-C.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.config import Config, load_env
from app.shop.providers import build_shop_providers
from app.shop.server import HOST, ShopApp, make_server
from app.shop.service import ShopService
from app.shop.store import DEFAULT_ROOT, SessionStore


def required_keys(config: Config) -> list[str]:
    """Environment variables live mode needs (names only; values are never printed).

    Lookups always use SerpApi, so its key is needed whichever provider searches.
    """
    keys = ["ANTHROPIC_API_KEY", "SERPAPI_API_KEY"]
    if config.search_provider == "serper":
        keys.insert(1, "SERPER_API_KEY")
    return keys


def cost_lines(config: Config) -> list[str]:
    credits = config.serper_credits_per_call
    per_credit = config.serper_usd_per_credit
    call_usd = config.serper_usd_per_call
    return [
        "Expected cost (estimates, from the APP_*_USD and APP_SERPER_* settings):",
        f"  Serper bills {credits} credits per call at ${per_credit:.4f} a credit "
        f"(${call_usd:.4f} a call).",
        "  Search: 1 call ("
        f"{credits} credits, ${call_usd:.4f}), or 2 if fewer than 24 results survive "
        f"({2 * credits} credits, ${2 * call_usd:.4f}); Anthropic about $0.01 or less "
        "(parse the request, rank the pool).",
        "  Shop Again: up to 3 Serper pages "
        f"({3 * credits} credits, ${3 * call_usd:.4f}); Anthropic about $0.01 or less "
        "(update the preference profile, rank); at most "
        f"{config.shop_agains_per_session} per session.",
        f"  Link lookups: none during a search. Each Open or Love runs one SerpApi lookup "
        f"(about ${config.serpapi_usd_per_search:.3f}), at most {config.shop_lookups_per_session} "
        "per session, never twice for the same result.",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (0 = any free)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_ROOT, help="session folder")
    args = parser.parse_args(argv)

    load_env()
    config = Config.from_env()
    if config.llm_mode == "live":
        missing = [k for k in required_keys(config) if not os.environ.get(k)]
        if missing:
            print(
                f"ERROR: live mode needs these variables set: {', '.join(missing)}", file=sys.stderr
            )
            return 2
    search, lookup = build_shop_providers(config)
    service = ShopService(config, SessionStore(args.data_dir), search, lookup)
    app = ShopApp(service)
    server = make_server(app, args.port)
    port = server.server_address[1]
    print(f"Shop Mode ({config.llm_mode}) at http://{HOST}:{port}/   (Ctrl-C to stop)", flush=True)
    if config.llm_mode == "live":
        print("\n".join(cost_lines(config)))
    else:
        print("Mock mode: fixture results, no network. Titles carry a [FIXTURE] prefix.")
    print(f"Sessions are saved in {args.data_dir}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
