"""Headless browser check of the Shop Mode page, in mock mode (no network, no key).

    make shop-check

Starts `scripts/shop.py` on a free port as a subprocess (mock mode, a temporary session folder),
drives the page with Playwright: the three example searches, three ratings (love, like, dislike), a
comment, Open on one result, then a reload to confirm the state is restored. Screenshots go to
spike-output/shop-screenshots/ (gitignored). The server is stopped when the check ends, whatever
the outcome. Every request that is not to the local server is answered by this script (product
thumbnails with a generated image, retailer pages with a stub), so nothing leaves the machine.

Needs Playwright for Python and a Chromium headless shell already installed (nothing is installed
here). `make shop-check` supplies the package with `uv run --with playwright`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Route, sync_playwright

from app.shop.replay import (
    REPLAY_FILE_2,
    REPLAY_FILE_3,
    REPLAY_FILE_4,
    REPLAY_FILE_EVIDENCE,
    seed_session,
)
from app.shop.store import SessionStore

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "spike-output" / "shop-screenshots"
EXAMPLES = [
    ("couches", "modern couches under $1000"),
    ("coffee-tables", "coffee tables with a mix of wood and metal with storage"),
    ("curtains", "boho curtains"),
]
COLORS = ["#c9d6df", "#e8d8c3", "#d4e0cf", "#e9cfd2", "#d9d3ea", "#f1e5b8"]
COFFEE = "coffee tables with a mix of wood and metal with storage"
PREMIUM = "Hartwell & Co"  # the invented premium seller in the mock fixtures
FAILS: list[str] = []
BET_IDS: dict[str, str] = {}  # stage 2s: the seeded sessions for the seller-bet check


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        FAILS.append(what)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def thumbnail_svg(url: str) -> str:
    n = int(hashlib.sha1(url.encode()).hexdigest()[:6], 16)
    bg, fg = COLORS[n % len(COLORS)], COLORS[(n // 7) % len(COLORS)]
    line = 'stroke="#555" stroke-width="3"'
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
        f'<rect width="200" height="200" fill="{bg}"/>'
        f'<rect x="40" y="95" width="120" height="48" rx="10" fill="{fg}" {line}/>'
        f'<rect x="30" y="75" width="140" height="30" rx="12" fill="{fg}" {line}/>'
        '<rect x="48" y="143" width="8" height="18" fill="#555"/>'
        '<rect x="144" y="143" width="8" height="18" fill="#555"/>'
        "</svg>"
    )


def install_routes(context: BrowserContext, port: int) -> list[str]:
    """Answer every non-local request here. Returns the list that records what was requested."""
    seen: list[str] = []
    counter = {"thumbs": 0}

    def handler(route: Route) -> None:
        url = route.request.url
        host = urlsplit(url).hostname or ""
        seen.append(host)
        if host == "mock.example":
            counter["thumbs"] += 1
            if counter["thumbs"] % 9 == 0:  # a dead thumbnail, to see the fallback tile
                route.abort()
            else:
                route.fulfill(status=200, content_type="image/svg+xml", body=thumbnail_svg(url))
        else:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=f"<title>Mock retailer</title><h1>Mock retailer page</h1><p>{host}</p>",
            )

    context.route(
        lambda url: (urlsplit(url).hostname or "") not in ("127.0.0.1", "localhost"), handler
    )
    return seen


def wait_for_results(page: Page) -> None:
    # Locator waits only: the page's CSP forbids the string evaluation wait_for_function uses.
    page.locator(".card").first.wait_for(timeout=15000)
    page.locator(".skeleton").first.wait_for(state="detached", timeout=15000)
    page.wait_for_load_state("networkidle")


def search(page: Page, text: str) -> None:
    page.fill("#request", text)
    page.click("#search-btn")
    wait_for_results(page)


def chips(page: Page) -> list[str]:
    return [t.strip() for t in page.locator("#chips .chip").all_inner_texts()]


def card_ids(page: Page, scope: str = "#grid") -> list[str]:
    return [c.get_attribute("data-id") or "" for c in page.locator(f"{scope} .card").all()]


def hold(page: Page, pattern: str) -> Callable[[], None]:
    """Hold the next request matching `pattern` (so a loading state can be looked at); the
    returned function lets it go and stops intercepting."""
    held: list[Route] = []

    def handler(route: Route) -> None:
        if held:
            route.continue_()
        else:
            held.append(route)

    page.route(pattern, handler)

    def release() -> None:
        held[0].continue_()
        page.unroute(pattern, handler)

    return release


def saved_counts(page: Page) -> tuple[str, str]:
    return page.inner_text("#tab-loved"), page.inner_text("#tab-liked")


def seller_cards(page: Page, seller: str, scope: str = "#grid") -> Any:
    return page.locator(f"{scope} .card").filter(has=page.locator(".seller", has_text=seller))


def count_seller(page: Page, seller: str) -> int:
    return int(seller_cards(page, seller).count())


def lookups_text(page: Page) -> str:
    return str(page.inner_text("#stats"))


def card_by_title(page: Page, needle: str, seller: str | None = None) -> Any:
    cards = page.locator("#grid .card").filter(has=page.locator(".title", has_text=needle))
    if seller is not None:
        cards = cards.filter(has=page.locator(".seller", has_text=seller))
    return cards.first


def title_texts(page: Page) -> list[str]:
    return [t for t in page.locator("#grid .card .title").all_inner_texts()]


def shape_of(title: str) -> str | None:
    for word in ("Round", "Rectangular", "Oval", "Square"):
        if word.lower() in title.lower():
            return word.lower()
    return None


def max_shape_share(page: Page) -> float:
    shapes = [shape_of(t) for t in title_texts(page)]
    known = [s for s in shapes if s]
    return max(known.count(s) for s in set(known)) / len(known) if known else 0.0


def seller_share(page: Page) -> tuple[str, float]:
    sellers = page.locator("#grid .card .seller").all_inner_texts()
    top = max(set(sellers), key=sellers.count)
    return top, sellers.count(top) / len(sellers)


def price_of(card: Any) -> float:
    text = card.locator(".price").first.inner_text().replace("$", "").replace(",", "")
    return float(text) if text.replace(".", "").isdigit() else 0.0


def wait_pref_chip(page: Page, text: str) -> Any:
    chip = page.locator("#pref-chips .chip.pref").filter(
        has=page.locator(".txt", has_text=re.compile(rf"^{re.escape(text)}$"))
    )
    chip.first.wait_for(timeout=10000)
    return chip.first


PREMIUM_NAMES = (
    "West Elm",
    "Article",
    "CB2",
    "Crate & Barrel",
    "Pottery Barn",
    "Room & Board",
    "Joybird",
)


def seed_replays(data_dir: str) -> dict[str, str]:
    """Put the owner's recorded session (and a copy whose preference call is forced to fail) in the
    server's session folder; the page opens them by id."""
    store = SessionStore(Path(data_dir))
    clean = seed_session(store)
    broken = seed_session(
        store,
        comments={"R1": "MOCK_TRUNCATE_ALL"},
        west_elm_extras=20,  # eight disliked results from other sellers give West Elm a like rate
        extra_ratings={
            "X20": ("dislike", "MOCK_TRUNCATE_ALL"),
            **{f"X{n}": ("dislike", "") for n in range(21, 29)},
        },
    )
    second = seed_session(store, replay=REPLAY_FILE_2)
    untagged = seed_session(  # the candidate tagging call fails (a mock-only marker in a title)
        store,
        replay=REPLAY_FILE_2,
        shown_titles={"Y1": "Rectangular Walnut Coffee Table MOCK_TAGS_FAIL"},
    )
    return {
        "replay": clean.session_id,
        "fallback": broken.session_id,
        "replay2": second.session_id,
        "untagged": untagged.session_id,
    }


def seed_replays_3(data_dir: str) -> dict[str, str]:
    """The owner's third session, and a copy where the candidate tagging call fails (the mock-only
    marker sits in a title in two of the shown results' batches, so both calls fail)."""
    store = SessionStore(Path(data_dir))
    # the owner liked 6 of 7: with that baseline no seller stands out (stage 2i), so ten shown
    # results from other sellers are disliked too
    dislikes = {f"S{n}": ("dislike", "") for n in range(1, 11)}
    clean = seed_session(store, replay=REPLAY_FILE_3, extra_ratings=dislikes)
    broken = seed_session(
        store,
        replay=REPLAY_FILE_3,
        extra_ratings=dislikes,
        shown_titles={
            "S1": "Rectangular Walnut Coffee Table MOCK_TAGS_FAIL",
            "S41": "Round Oak Coffee Table MOCK_TAGS_FAIL",
        },
    )
    return {"replay3": clean.session_id, "tagfail3": broken.session_id}


def seed_replays_4(data_dir: str) -> dict[str, str]:
    """The owner's fourth session, and a copy where the model's tagging answers come back empty for
    two of the shown results' batches (a mock-only marker in two titles)."""
    store = SessionStore(Path(data_dir))
    clean = seed_session(store, replay=REPLAY_FILE_4)
    broken = seed_session(
        store,
        replay=REPLAY_FILE_4,
        shown_titles={
            "E1": "Rectangular Walnut Coffee Table MOCK_TAGS_EMPTY",
            "E40": "Round Oak Coffee Table MOCK_TAGS_EMPTY",
        },
    )
    return {"replay4": clean.session_id, "empty4": broken.session_id}


def open_session(browser: Any, base: str, session_id: str, width: int = 1280) -> tuple[Any, Page]:
    context = browser.new_context(viewport={"width": width, "height": 900})
    install_routes(context, 0)
    context.add_init_script(
        "if (!window.localStorage.getItem('shop.session')) "
        f"window.localStorage.setItem('shop.session', '{session_id}');"
    )
    page = context.new_page()
    page.goto(base)
    wait_for_results(page)
    return context, page


def run_replay(
    browser: Any, base: str, shots: Path, ids: dict[str, str], console_errors: list[str]
) -> None:
    print("the owner's real session, replayed through the page")
    context, page = open_session(browser, base, ids["replay"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    request_chips = chips(page)
    before = title_texts(page)
    check(len(before) == 24, f"{len(before)} results restored from the recorded search")
    check(page.locator("#shop-again").is_enabled(), "Shop Again is enabled (10 ratings recorded)")
    check(page.locator("#why-toggle").is_hidden(), "no How I decided before the first Shop Again")
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#prefs").wait_for(state="visible")
    shape_chip = wait_pref_chip(page, "shape: open")
    check(
        "liked rectangular and round" in shape_chip.inner_text(),
        "shape is open, round and rectangular",
    )
    price_chip = wait_pref_chip(page, "price: mixed")
    check(
        "loved $699, liked" in price_chip.inner_text()
        and "liked $144 to $799" in price_chip.inner_text(),
        "price is mixed",
    )
    seller_chip = page.locator("#pref-chips .chip.pref").filter(
        has=page.locator(".txt", has_text="seller: West Elm")
    )
    check(
        seller_chip.count() == 0,
        "no West Elm chip: all ten rated items were liked, so nothing beats the 100% baseline",
    )
    like_text = " ".join(
        page.locator("#pref-chips .chip.pref.inferred:not(.neutral) .txt").all_inner_texts()
    ).lower()
    check(
        "round" not in like_text and "industrial" not in like_text,
        "no round or industrial like chip",
    )
    check(chips(page) == request_chips, "the request chips are unchanged")
    summary = page.inner_text("#prefs-summary")
    check("Shape is open" in summary and "Price is mixed." in summary, f"summary: {summary}")
    batch = title_texts(page)
    check(not set(batch) & set(before), "the Shop Again batch shows nothing seen before")
    check(
        max_shape_share(page) <= 0.6,
        f"no shape above 60% of the batch ({max_shape_share(page):.0%})",
    )
    top, share = seller_share(page)
    check(share <= 0.5, f"no unpinned seller above 50% of the batch ({top} {share:.0%})")
    print("   footer:", lookups_text(page))
    check("tagged 10 of 10 items" in lookups_text(page), "footer shows items tagged 10 of 10")
    check("candidates tagged" in lookups_text(page), "footer shows candidates tagged N of M")
    page.screenshot(path=str(shots / "12-replay-chips.png"))

    toggle = page.locator("#why-toggle")
    check(
        toggle.is_visible() and page.locator("#why").is_hidden(),
        "How I decided is shown and closed by default",
    )
    toggle.click()
    why = page.locator("#why")
    check(why.is_visible(), "the panel opens")
    names = [t.lower() for t in why.locator(".why-dim h4").all_inner_texts()]
    check(len(names) >= 8, f"the panel lists {len(names)} decisions")
    check(any(n.startswith("batch makeup") for n in names), "the panel lists the batch makeup")
    check(
        any(n.startswith("shape: open") for n in names)
        and any(n.startswith("price: mixed") for n in names),
        "shape open and price mixed are listed",
    )
    check(any(n.startswith("seller: none") for n in names), "the seller decision is listed")
    check(
        any(n.startswith("like-rate evidence") for n in names),
        "the like-rate evidence table is listed",
    )
    text = why.inner_text()
    check(
        "known coverage" in text and "like mass" in text and "lift" in text, "the numbers are shown"
    )
    check(
        "would hold 86% of the like mass and 75% of the liked items" in text,
        "the reason price is not a band is shown",
    )
    page.screenshot(path=str(shots / "13-replay-how-i-decided.png"), full_page=True)
    page.reload()
    wait_for_results(page)
    check(page.locator("#why").is_visible(), "the panel's open state is remembered after a reload")
    page.set_viewport_size({"width": 390, "height": 844})
    check(
        page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 1,
        "no sideways scroll at 390 px",
    )
    page.screenshot(path=str(shots / "14-replay-mobile.png"))
    page.set_viewport_size({"width": 1280, "height": 900})
    page.screenshot(path=str(shots / "15-replay-grid.png"))
    context.close()

    run_replay_2(browser, base, shots, ids, console_errors)

    print("the same session with the preference call forced to fail")
    context, page = open_session(browser, base, ids["fallback"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#prefs-note").wait_for(state="visible")
    check(
        page.inner_text("#prefs-note")
        == "Could not read item details; only seller and price were learned.",
        "the fallback banner appears",
    )
    chip_text = page.inner_text("#pref-chips")
    check(
        "shape" not in chip_text
        and "open" not in chip_text.lower()
        and "seller: West Elm" in chip_text,
        "only seller and price chips remain",
    )
    check(
        "tagged 0 of 19 items" in lookups_text(page) and "fallback" in lookups_text(page),
        "footer says the details were not read",
    )
    page.locator("#why-toggle").click()
    check("Mode: fallback" in page.locator("#why").inner_text(), "the panel says fallback")
    page.screenshot(path=str(shots / "16-fallback-banner.png"), full_page=True)
    context.close()


def run_replay_2(
    browser: Any, base: str, shots: Path, ids: dict[str, str], console_errors: list[str]
) -> None:
    print("the owner's second session (Home Depot, 91% rectangular), replayed through the page")
    context, page = open_session(browser, base, ids["replay2"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    request_chips = chips(page)
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#prefs").wait_for(state="visible")
    shape_chip = wait_pref_chip(page, "shape: open")
    check("liked rectangular and round" in shape_chip.inner_text(), "shape is open")
    wait_pref_chip(page, "price: mixed")
    wait_pref_chip(page, "warm wood")
    check(
        page.locator("#pref-chips .chip.pref")
        .filter(has=page.locator(".txt", has_text="seller:"))
        .count()
        == 0,
        "no seller chip (Home Depot is 25% of the like mass, under 40%)",
    )
    summary = page.inner_text("#prefs-summary")
    check("more pieces from" not in summary and "Home Depot" not in summary, f"summary: {summary}")
    check(chips(page) == request_chips, "the request chips are unchanged")
    titles = title_texts(page)
    shapes = [shape_of(t) for t in titles]
    check(
        shapes.count("round") >= 6 and shapes.count("rectangular") >= 6,
        f"both shapes are in the batch ({shapes.count('round')} round, "
        f"{shapes.count('rectangular')} rectangular)",
    )
    check(
        max_shape_share(page) <= 0.6,
        f"no shape above 60% of the batch ({max_shape_share(page):.0%})",
    )
    top, share = seller_share(page)
    check(share <= 0.5, f"no seller above 50% of the batch ({top} {share:.0%})")
    foot = lookups_text(page)
    print("   footer:", foot)
    check(
        "candidates tagged" in foot and "tagged 9 of 9 items" in foot,
        "footer shows the tagging counts",
    )
    page.screenshot(path=str(shots / "17-replay2-chips.png"))
    page.locator("#why-toggle").click()
    why = page.locator("#why")
    check(why.is_visible(), "the panel opens")
    text = why.inner_text()
    check("batch makeup (24 results)" in text.lower(), "the panel shows the batch makeup")
    check(
        "pool before" in text and "final batch" in text and "slots planned" in text,
        "pool, slots and batch columns",
    )
    check("shown results tagged 35 of 35 (model)" in text, "the shown-result tag coverage is shown")
    check(
        "candidates tagged" in text and "Searches used:" in text,
        "candidate tagging and searches are shown",
    )
    check("rated 2, needs 4" in text, "the Home Depot rule that failed is shown")
    check("lift 1.25 for wood is under 1.3" in text, "the material lift is shown")
    page.screenshot(path=str(shots / "18-replay2-trace.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    check(
        page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 1,
        "no sideways scroll at 390 px with the panel open",
    )
    page.screenshot(path=str(shots / "20-replay2-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator("#why-toggle").click()
    page.screenshot(path=str(shots / "19-replay2-grid.png"))
    context.close()

    print("the same session with the candidate tagging call failing")
    context, page = open_session(browser, base, ids["untagged"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.click("#shop-again")
    wait_for_results(page)
    check(
        "Candidate tagging failed:" in page.inner_text("#status"),
        "the page says candidate tagging failed, with the reason",
    )
    chip_text = page.inner_text("#pref-chips")
    check("warm wood" not in chip_text, "no finish chip without the model's tags of what was shown")
    check(len(title_texts(page)) > 0, "a batch is still shown")
    page.locator("#why-toggle").click()
    check(
        "(keywords)" in page.locator("#why").inner_text(),
        "the panel says the baseline is title keywords",
    )
    page.screenshot(path=str(shots / "21-tagging-failed.png"), full_page=True)
    context.close()


def run_replay_3(
    browser: Any, base: str, shots: Path, ids: dict[str, str], console_errors: list[str]
) -> None:
    print("the owner's third session (West Elm, design retailer peers), replayed through the page")
    context, page = open_session(browser, base, ids["replay3"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    request_chips = chips(page)
    before = title_texts(page)
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#prefs").wait_for(state="visible")
    seller_chip = wait_pref_chip(page, "seller: West Elm")
    check(
        "inferred" in seller_chip.get_attribute("class"),
        "the West Elm seller chip appears despite one dislike (net rule)",
    )
    tier_chip = wait_pref_chip(page, "retailers like West Elm")
    check("inferred" in tier_chip.get_attribute("class"), "the retailer tier chip appears")
    wait_pref_chip(page, "modern-leaning style")
    check("converged" not in page.inner_text("#pref-chips"), "no raw status words in the chips")
    check(
        "style: open" not in page.inner_text("#pref-chips"),
        "style is a converged modern family, not open",
    )
    wait_pref_chip(page, "shape: open")
    wait_pref_chip(page, "price: mixed")
    check(chips(page) == request_chips, "the request chips are unchanged")
    summary = page.inner_text("#prefs-summary")
    check(
        "West Elm and similar retailers" in summary and "modern-leaning" in summary,
        f"summary: {summary}",
    )
    batch = title_texts(page)
    check(not set(batch) & set(before), "the Shop Again batch shows nothing seen before")
    sellers = page.locator("#grid .card .seller").all_inner_texts()
    peers_in = {s for s in sellers if s in ("West Elm", "Article", "CB2")}
    check(len(peers_in) == 3, f"the batch includes peer retailers {sorted(peers_in)}")
    check(
        sum(1 for s in sellers if s in PREMIUM_NAMES) <= 12,
        "design premium retailers hold at most half of the batch",
    )
    check(max(sellers.count(s) for s in set(sellers)) <= 12, "no seller above half the batch")
    check(
        max_shape_share(page) <= 0.6,
        f"no shape above 60% of the batch ({max_shape_share(page):.0%})",
    )
    foot = lookups_text(page)
    print("   footer:", foot)
    check("candidates tagged" in foot, "footer shows candidates tagged N of M")
    check("affinity tier" in foot and " sellers" in foot, "footer shows the tier share and sellers")
    check("matches your preferences:" in foot and "n/a" not in foot, "alignment is a real count")
    import re as _re

    m = _re.search(r"candidates tagged (\d+) of (\d+)", foot)
    check(
        m is not None and min(48, int(m.group(2))) <= int(m.group(1)) <= int(m.group(2)),
        "most of the 60 tagged candidates (some are collapsed after tagging) are kept and tagged",
    )
    page.screenshot(path=str(shots / "22-replay3-chips.png"))

    page.locator("#why-toggle").click()
    why = page.locator("#why")
    text = why.inner_text()
    check(
        "Peer retailer Article" in text and "Peer retailer CB2" in text,
        "the peers are in the trace",
    )
    check(
        _re.search(r"Peer retailer Article: searched .* found 8, 8 new", text) is not None,
        "with their new candidates",
    )
    check("Affinity tier design premium: up to 12" in text, "the tier cap is in the trace")
    check("retailer tier" in text and "final batch" in text, "the tier makeup table is shown")
    check("labels: " in text and "industrial" in text, "the style leaves are in the trace")
    check(
        "tagging call" in text.lower() or "candidates tagged" in text, "tagging coverage is shown"
    )
    page.screenshot(path=str(shots / "23-replay3-trace.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    check(
        page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 1,
        "no sideways scroll at 390 px with the panel open",
    )
    page.screenshot(path=str(shots / "25-replay3-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator("#why-toggle").click()
    page.screenshot(path=str(shots / "24-replay3-grid.png"))
    context.close()

    print("the same session with candidate tagging failing in both halves")
    context, page = open_session(browser, base, ids["tagfail3"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.click("#shop-again")
    wait_for_results(page)
    status = page.inner_text("#status")
    check("Candidate tagging failed:" in status, f"the page shows the real reason: {status[:140]}")
    check("no text content" in status, "the reason names the empty answer")
    check("warm wood" not in page.inner_text("#pref-chips"), "no attribute chip without tags")
    page.locator("#why-toggle").click()
    why = page.locator("#why").inner_text()
    check(
        "Tagging call 1" in why and "stop max_tokens" in why, "the tagging calls are in the trace"
    )
    check("error: llm.shop_candidate_tags" in why, "each failed call says why")
    page.screenshot(path=str(shots / "26-replay3-tagging-failed.png"), full_page=True)
    context.close()


def run_replay_4(
    browser: Any, base: str, shots: Path, ids: dict[str, str], console_errors: list[str]
) -> None:
    print("the owner's fourth session (46 ratings, duplicate listings), replayed through the page")
    context, page = open_session(browser, base, ids["replay4"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    # the mock answers in milliseconds, so the first two polls are replaced by running phases to see
    # what the page does with a phase text; the third is the real answer
    phases = ["Reading your ratings", "Tagging results"]
    polled = {"n": 0}

    def fake_phase(route: Route) -> None:
        if polled["n"] < len(phases):
            body = json.dumps(
                {"state": "running", "phase": phases[polled["n"]], "kind": "shop_again"}
            )
            polled["n"] += 1
            route.fulfill(status=200, content_type="application/json", body=body)
        else:
            route.continue_()

    page.route("**/api/job/**", fake_phase)
    request_chips = chips(page)
    before = title_texts(page)
    page.click("#shop-again")
    page.locator("#status").filter(has_text="Reading your ratings").wait_for(timeout=10000)
    check(page.locator("#stepbar").is_visible(), "the spinner is shown while the job runs")
    check("Reading your ratings" in page.inner_text("#status"), "the first phase is shown")
    page.screenshot(path=str(shots / "27-replay4-spinner-phase.png"))
    page.locator("#status").filter(has_text="Tagging results").wait_for(timeout=10000)
    check("Tagging results" in page.inner_text("#status"), "the next phase replaces it")
    wait_for_results(page)
    page.locator("#prefs").wait_for(state="visible")
    status = page.inner_text("#status")
    check(status == "24 new results.", f"the banner is one short sentence: {status!r}")
    check(len(status) < 120 and status.count(". ") == 0, "and not a paragraph")
    check(page.locator("#status-details").is_visible(), "with a details link")
    wait_pref_chip(page, "shape: open")
    check(
        "seller:" not in page.inner_text("#pref-chips")
        and "retailers like" not in page.inner_text("#pref-chips"),
        "no seller or retailer chip (West Elm 3 rated, the premium tier liked 6 of 28: 21%)",
    )
    check(chips(page) == request_chips, "the request chips are unchanged")
    page.screenshot(path=str(shots / "28-replay4-short-banner.png"))
    batch = title_texts(page)
    check(len(batch) == 24 and len(set(batch)) == 24, "24 distinct titles in the grid")
    check(not set(batch) & set(before), "nothing already shown comes back")
    imgs = page.locator("#grid .card .thumb img")
    srcs = [imgs.nth(i).get_attribute("src") for i in range(imgs.count())]
    check(len(srcs) == len(set(srcs)), f"no thumbnail appears twice ({len(srcs)} thumbnails)")
    check(
        len(
            {
                (t.lower(), p)
                for t, p in zip(
                    batch, page.locator("#grid .card .price").all_inner_texts(), strict=True
                )
            }
        )
        == 24,
        "no title and price appears twice",
    )
    foot = lookups_text(page)
    print("   footer:", foot)
    check(
        "searches " in foot and "preferences " in foot and "rank " in foot,
        "footer shows phase timings",
    )
    check("candidates tagged" in foot, "and the candidates tagged")
    page.locator("#status-details").click()
    why = page.locator("#why")
    check(why.is_visible(), "the details link opens How I decided")
    text = why.inner_text()
    check(
        re.search(r"Time: searches [\d.]+ s, preferences [\d.]+ s", text) is not None,
        "phase timings are in the trace",
    )
    check("aim 30.0 s, hard stop 45.0 s" in text, "with the target and the hard stop")
    check(
        re.search(
            r"Candidates \d+: kept \d+; seen before: \d+ shown, \d+ rated; duplicates \d+", text
        )
        is not None,
        "de-duplication counts are in the trace",
    )
    check("same product id" in text, "per rule")
    check("Model-written queries:" in text, "the model-written queries are in the trace")
    check("like-rate evidence" in text.lower(), "and the like-rate evidence table")
    check("Tags behind the batch:" in text, "and where the batch's tags came from")
    page.screenshot(path=str(shots / "29-replay4-trace.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    check(
        page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 1,
        "no sideways scroll at 390 px",
    )
    page.screenshot(path=str(shots / "31-replay4-mobile.png"))
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator("#why-toggle").click()
    page.screenshot(path=str(shots / "30-replay4-grid.png"))
    context.close()

    print("the same session with the model's tagging answers coming back empty")
    context, page = open_session(browser, base, ids["empty4"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    t0 = time.monotonic()
    page.click("#shop-again")
    wait_for_results(page)
    took = time.monotonic() - t0
    status = page.inner_text("#status")
    check(took < 20, f"it still completes quickly ({took:.1f} s)")
    check(len(title_texts(page)) == 24, "and shows a full batch")
    check(
        "Candidate tagging failed:" in status and "no text content" in status,
        f"the reason shows: {status[:150]}",
    )
    check(status.count(". ") <= 2, "in one or two short sentences")
    page.screenshot(path=str(shots / "32-replay4-tagging-empty.png"))
    context.close()


def run_checks(
    base: str,
    shots: Path,
    replay_ids: dict[str, str],
    base3: str,
    ids3: dict[str, str],
    base4: str,
    ids4: dict[str, str],
    base5: str,
    ids5: dict[str, str],
    base6: str,
    id6: str,
    id7: str,
    base7: str,
    base8: str,
    scenarios: dict[str, str],
) -> None:
    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            install_routes(context, 0)
            page = context.new_page()
            page.on(
                "console",
                lambda m: (
                    console_errors.append(m.text)
                    if m.type == "error" and "Failed to load resource" not in m.text
                    else None
                ),
            )
            page.on("pageerror", lambda e: console_errors.append(str(e)))

            print("empty page")
            page.goto(base)
            page.wait_for_selector("#examples button")
            check(
                page.locator("#shop-again").is_disabled(), "Shop Again is disabled before a rating"
            )
            page.screenshot(path=str(shots / "01-empty.png"))

            print("scenario A: one round and one rectangular item loved from one seller")
            release = hold(page, "**/api/search")
            page.fill("#request", COFFEE)
            page.click("#search-btn")
            page.locator("#stepbar").wait_for(state="visible")
            check(page.inner_text("#status") == "Searching", "loading text")
            page.screenshot(path=str(shots / "02-loading.png"))
            release()
            wait_for_results(page)
            first_titles = title_texts(page)
            request_chips = chips(page)
            print("   request chips:", " | ".join(request_chips).replace("\n", " "))
            check(len(first_titles) == 24, f"{len(first_titles)} results in the first batch")
            page.screenshot(path=str(shots / "03-scenario-a-first-grid.png"))

            round_love = card_by_title(page, "Round Walnut Drum", PREMIUM)
            rect_love = card_by_title(page, "Rectangular Wood and Black Metal", PREMIUM)
            round_love.locator(".rate.love").click()
            rect_love.locator(".rate.love").click()
            others = page.locator("#grid .card").filter(
                has_not=page.locator(".seller", has_text=PREMIUM)
            )
            round_others = others.filter(has=page.locator(".title", has_text="Round"))
            rect_others = others.filter(has=page.locator(".title", has_text="Rectangular"))
            check(
                round_others.count() >= 1 and rect_others.count() >= 1,
                "items of both shapes from other sellers were shown",
            )
            cheap_round = min(
                (round_others.nth(i) for i in range(round_others.count())), key=price_of
            )
            dear_rect = max((rect_others.nth(i) for i in range(rect_others.count())), key=price_of)
            low, high = price_of(cheap_round), price_of(dear_rect)
            print(f"   liked prices ${low:,.0f} and ${high:,.0f}")
            cheap_round.locator(".rate.like").click()
            dear_rect.locator(".rate.like").click()
            oval = (
                page.locator("#grid .card")
                .filter(has=page.locator(".title", has_text="Oval"))
                .filter(has_not=page.locator(".seller", has_text=PREMIUM))
                .first
            )
            oval.locator(".rate.dislike").click()
            # a seller chip needs 4 rated and 2 liked from the seller at 1.5 times the overall like
            # rate (stage 2i): love the other two premium items and dislike four more results
            card_by_title(page, "Round Brushed Brass Drum", PREMIUM).locator(".rate.love").click()
            card_by_title(page, "Rectangular Walnut", PREMIUM).locator(".rate.love").click()
            for word, nth in (("Square", 0), ("Square", 1), ("Oval", 1), ("Oval", 2)):
                others.filter(has=page.locator(".title", has_text=word)).nth(nth).locator(
                    ".rate.dislike"
                ).click()
            page.wait_for_load_state("networkidle")
            check("link lookups 0 of 12" in lookups_text(page), "loving and liking ran no lookup")

            print("Shop Again")
            page.click("#shop-again")
            wait_for_results(page)
            page.locator("#prefs").wait_for(state="visible")
            batch1 = title_texts(page)
            check(not set(batch1) & set(first_titles), "batch 1 shows nothing seen before")
            check(
                chips(page) == request_chips, "the request chips are unchanged after Shop Again 1"
            )
            open_shape = wait_pref_chip(page, "shape: open")
            check("neutral" in open_shape.get_attribute("class"), "an open shape chip")
            check("rectangular and round" in open_shape.inner_text(), "it lists both liked shapes")
            check(
                "round"
                not in " ".join(
                    page.locator("#pref-chips .chip.pref:not(.open) .txt").all_inner_texts()
                ).lower(),
                "no chip pushes round or rectangular",
            )
            check(
                "Shape is open" in page.inner_text("#prefs-summary"),
                "the summary says shape is open",
            )
            price_chip = wait_pref_chip(page, "price: mixed")
            check(
                "loved $" in price_chip.inner_text() and "liked $" in price_chip.inner_text(),
                "price shows as mixed",
            )
            check(
                "Price is mixed." in page.inner_text("#prefs-summary"),
                "the summary says price is mixed",
            )
            check(
                page.locator("#pref-chips .chip.pref.inferred", has_text="wood").count() >= 1,
                "a converged wood chip",
            )
            check(
                page.locator("#pref-chips .chip.pref.inferred", has_text="seller: Hartwell").count()
                == 1,
                "the premium seller chip (4 loved items, a like rate over 1.5 times the baseline)",
            )
            seller_chip = page.locator("#pref-chips .chip.pref").filter(
                has=page.locator(".txt", has_text=f"seller: {PREMIUM}")
            )
            check(seller_chip.count() == 1, "the seller chip")
            tag_open = page.locator("#pref-chips .chip.pref.neutral .tag").first.text_content()
            tag_inf = page.locator(
                "#pref-chips .chip.pref.inferred:not(.open) .tag"
            ).first.text_content()
            check(
                tag_open in ("open", "mixed") and tag_inf == "inferred",
                f"tags {tag_open!r} {tag_inf!r}",
            )
            check(
                open_shape.evaluate("e => getComputedStyle(e).borderStyle") == "solid"
                and seller_chip.evaluate("e => getComputedStyle(e).borderStyle") == "dashed",
                "open chips are solid, inferred dashed (guess chips are dotted)",
            )
            open_shape.hover()
            tip = open_shape.locator(".tip")
            check(tip.is_visible() and "Open:" in tip.inner_text(), "the open chip tooltip shows")
            check("Round Walnut Drum" in tip.inner_text(), "it lists evidence titles as text")
            page.screenshot(
                path=str(shots / "04-open-chip-tooltip.png"), clip=_clip(page, open_shape)
            )
            page.mouse.move(5, 500)
            price_chip.hover()
            page.screenshot(
                path=str(shots / "05-price-mixed-chip.png"), clip=_clip(page, price_chip)
            )
            page.mouse.move(5, 500)

            top1, share1 = seller_share(page)
            max_shape = max(max_shape_share(page), 0.0)
            print(
                f"   batch 2: top seller {top1} {share1:.0%}, largest shape share {max_shape:.0%}"
            )
            check(share1 <= 0.5, "no unpinned seller above 50% of the batch")
            check(max_shape <= 0.6, "no shape above 60% of the batch")
            check(
                "largest share:" in lookups_text(page) and "shape" in lookups_text(page),
                "footer shows the largest shares",
            )

            print("pin the seller")
            page.locator("#pref-chips .chip.pref .pinbtn").click()
            pinned_chip = page.locator("#pref-chips .chip.pref").filter(
                has=page.locator(".pinned-tag")
            )
            pinned_chip.first.wait_for(timeout=10000)
            check(
                page.locator("#pref-chips .pinbtn").get_attribute("aria-pressed") == "true",
                "the seller chip shows it is pinned",
            )
            page.screenshot(
                path=str(shots / "06-pinned-seller-chip.png"), clip=_clip(page, pinned_chip.first)
            )
            page.click("#shop-again")
            wait_for_results(page)
            batch2 = title_texts(page)
            check(
                not set(batch2) & (set(first_titles) | set(batch1)),
                "the second batch shows nothing seen before",
            )
            top2, share2 = seller_share(page)
            print(f"   pinned batch: top seller {top2} {share2:.0%}")
            # stage 2s: a pin raises the seller's ceiling to 80% but the allowance still has to
            # be earned batch by batch, so it is no longer "more than half" at once
            check(
                share2 <= 0.8,
                "a pinned seller stays within its allowance, never more than 80%",
            )
            check(
                chips(page) == request_chips,
                "the request chips are unchanged after the second Shop Again",
            )
            page.screenshot(path=str(shots / "07-scenario-a-grid-after.png"))

            print("Saved panel wraps")
            page.click("#saved-toggle")
            check(page.locator("#saved").is_visible(), "Saved opens")
            check(saved_counts(page)[0].startswith("Loved (4)"), f"tabs {saved_counts(page)}")
            page.click("#tab-liked")
            page.screenshot(path=str(shots / "08-saved-panel.png"))
            row = page.locator("#saved-row")
            overflow = row.evaluate("e => e.scrollWidth - e.clientWidth")
            check(overflow <= 1, f"Saved panel does not scroll sideways ({overflow}px)")
            check(
                row.evaluate("e => getComputedStyle(e).display") == "grid",
                "Saved panel is a grid like the results",
            )
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#saved").scroll_into_view_if_needed()
            check(
                page.locator("#saved-row").evaluate("e => e.scrollWidth - e.clientWidth") <= 1
                and page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 1,
                "no sideways scroll at 390 px",
            )
            page.screenshot(path=str(shots / "09-mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 900})
            page.click("#saved-toggle")

            print("reload")
            page.reload()
            wait_for_results(page)
            check("shape: open" in page.inner_text("#pref-chips"), "the open chip is restored")
            check(page.locator("#pref-chips .pinned-tag").count() == 1, "the pin is restored")
            check(chips(page) == request_chips, "the request chips are restored unchanged")

            print("scenario B: two round items loved, one rectangular liked")
            page.click("#start-over")
            page.click("#start-over-yes")
            search(page, COFFEE)
            card_by_title(page, "Round Walnut Drum", PREMIUM).locator(".rate.love").click()
            card_by_title(page, "Round Brushed Brass Drum", PREMIUM).locator(".rate.love").click()
            rects = (
                page.locator("#grid .card")
                .filter(has=page.locator(".title", has_text="Rectangular"))
                .filter(has_not=page.locator(".seller", has_text=PREMIUM))
            )
            rects.nth(0).locator(".rate.like").click()  # one rectangular: round holds 86% (>= 80%)
            page.locator("#grid .card").filter(has=page.locator(".title", has_text="Oval")).filter(
                has_not=page.locator(".seller", has_text=PREMIUM)
            ).first.locator(".rate.dislike").click()
            page.wait_for_load_state("networkidle")
            first_b = title_texts(page)
            first_round_share = sum(1 for t in first_b if shape_of(t) == "round") / len(first_b)
            page.screenshot(path=str(shots / "10-scenario-b-first-grid.png"))
            page.click("#shop-again")
            wait_for_results(page)
            page.locator("#prefs").wait_for(state="visible")
            round_chip = wait_pref_chip(page, "round")
            check("inferred" in round_chip.get_attribute("class"), "shape converges on round")
            check("shape: open" not in page.inner_text("#pref-chips"), "and shape is not open")
            batch = title_texts(page)
            rounds = sum(1 for t in batch if shape_of(t) == "round")
            print(f"   scenario B batch: {rounds} of {len(batch)} round")
            again_share = rounds / len(batch)
            print(
                f"   round share: first batch {first_round_share:.0%}, Shop Again {again_share:.0%}"
            )
            check(
                rounds / len(batch) > first_round_share + 0.05,
                "the batch leans more round than the first one",
            )
            page.screenshot(path=str(shots / "11-scenario-b-grid-after.png"))

            run_replay(browser, base, shots, replay_ids, console_errors)
            run_replay_3(browser, base3, shots, ids3, console_errors)
            run_replay_4(browser, base4, shots, ids4, console_errors)
            run_progress(browser, base5, shots, ids5, console_errors)
            run_tailoring(browser, base6, shots, id6, console_errors)
            run_batch(browser, base, base6, id7, shots, console_errors)
            run_fixes(browser, base7, shots, console_errors)
            run_open(browser, base8, shots, console_errors)
            run_dry(browser, scenarios, shots, console_errors)
            run_fixes_2q(browser, scenarios, shots, console_errors)
            run_fixes_2r(browser, scenarios, shots, console_errors)
            run_seller_bets(browser, scenarios["bets"], BET_IDS, shots, console_errors)
            run_local_unchanged(browser, base, shots, console_errors)
            check(console_errors == [], f"no console or page errors {console_errors[:3]}")
        finally:
            browser.close()


def check_bar(page: Page, expect_ids: list[str], what: str) -> None:
    box = page.locator("#stepbar")
    check(box.get_attribute("role") == "progressbar", f"{what}: the bar is a progressbar")
    steps = page.locator("#steps .step")
    check(steps.count() == len(expect_ids), f"{what}: {steps.count()} steps")
    now, top = box.get_attribute("aria-valuenow"), box.get_attribute("aria-valuemax")
    check(top == str(len(expect_ids)) and now is not None, f"{what}: aria {now} of {top}")
    live = page.locator("#status")
    check(live.get_attribute("aria-live") == "polite", f"{what}: the step text is a polite region")
    check(bool(live.inner_text().strip()), f"{what}: the step text reads '{live.inner_text()}'")


def run_progress(
    browser: Any, base: str, shots: Path, ids: dict[str, str], console_errors: list[str]
) -> None:
    print("the step bar, with each mock step slowed down so the run can be seen")
    context, page = open_session(browser, base, ids["replay4"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.fill("#request", COFFEE)
    page.click("#search-btn")
    page.locator("#steps .step.active").first.wait_for(timeout=15000)
    page.locator("#steps .step.skipped").first.wait_for(timeout=15000)
    check_bar(page, ["searching", "tagging", "ranking"], "first search")
    check(page.locator("#elapsed").inner_text().endswith(" s"), "the elapsed seconds are shown")
    check(page.locator("#spinner").count() == 0, "there is no spinner")
    page.screenshot(path=str(shots / "33-progress-search.png"))
    wait_for_results(page)
    check(page.locator("#stepbar").is_hidden(), "the bar is gone when the results arrive")
    context.close()
    context, page = open_session(browser, base, ids["replay4"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.click("#shop-again")
    page.locator("#steps .step.active").first.wait_for(timeout=15000)
    page.locator("#steps .step.done, #steps .step.skipped").first.wait_for(timeout=15000)
    check_bar(page, ["ratings", "searching", "tagging", "ranking"], "Shop Again")
    page.screenshot(path=str(shots / "34-progress-shop-again.png"))
    page.emulate_media(color_scheme="dark")
    page.screenshot(path=str(shots / "35-progress-shop-again-dark.png"))
    page.set_viewport_size({"width": 390, "height": 800})
    page.screenshot(path=str(shots / "36-progress-shop-again-mobile.png"))
    wait_for_results(page)
    check(page.locator("#stepbar").is_hidden(), "the bar is gone when Shop Again is done")
    context.close()


MARKETPLACE_SELLERS = ("Walmart", "Lowe's", "Home Depot", "Overstock", "Amazon")


def run_tailoring(
    browser: Any, base: str, shots: Path, session_id: str, console_errors: list[str]
) -> None:
    print("the evidence session (48 rated, 15 liked): chips, evidence table, exclusion")
    context, page = open_session(browser, base, session_id)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    request_chips = chips(page)
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#prefs").wait_for(state="visible")
    toward = page.locator("#pref-chips .chip.pref.lean-toward")
    away = page.locator("#pref-chips .chip.pref.lean-away")
    market = page.locator("#pref-chips .chip.pref.lean-filter")
    toward.first.wait_for(timeout=10000)
    check(
        toward.filter(has=page.locator(".txt", has_text="natural wood (frame color)")).count() == 1,
        "leaning toward natural wood (frame)",
    )
    check(
        away.filter(has=page.locator(".txt", has_text="black (frame color)")).count() == 1,
        "leaning away from black (frame)",
    )
    check(market.count() == 1 and "liked 1 of 11" in market.inner_text(), "the exclusion chip")
    check(
        page.locator("#pref-chips .chip.pref", has_text="seller: ").count() == 3,
        "three seller chips (West Elm, Article, CB2)",
    )
    check("retailers like" in page.inner_text("#pref-chips"), "and the retailer tier chip")
    check(
        toward.first.evaluate("e => getComputedStyle(e).borderStyle") == "dashed"
        and away.first.evaluate("e => getComputedStyle(e).borderStyle") == "dashed",
        "leaning chips have a dashed outline",
    )
    check(chips(page) == request_chips, "the request chips are unchanged")
    sellers = page.locator("#grid .card .seller").all_inner_texts()
    check(
        not [s for s in sellers if s in MARKETPLACE_SELLERS],
        f"no marketplace seller in the batch ({len(sellers)} results)",
    )
    liked = [s for s in sellers if s in ("West Elm", "Article", "CB2")]
    check(len(liked) >= 6, f"{len(liked)} results from the liked sellers")
    foot = lookups_text(page)
    print("   footer:", foot)
    check("(counted: " in foot and "leaning toward" in foot, "the footer says what it counted")
    check("nothing decided yet" not in foot, "and does not say nothing is decided")
    toward.first.hover()
    check("Leaning toward" in toward.first.locator(".tip").inner_text(), "the leaning tooltip")
    page.mouse.move(5, 600)
    page.locator("#prefs").screenshot(path=str(shots / "37-tailoring-chips.png"))
    page.locator("#why-toggle").click()
    why = page.locator("#why")
    table = why.locator(".why-evidence")
    table.scroll_into_view_if_needed()
    text = table.inner_text()
    check("natural wood" in text and "frame color" in text and "leaning toward" in text,
          "the evidence table lists the frame values with a verdict")  # fmt: skip
    check("West Elm" in text and "design premium" in text, "and the sellers and the tiers")
    check("rated" in text.lower() and "like rate" in text.lower(), "with rated, liked and rates")
    check("Model-written queries:" in why.inner_text(), "the written queries are in the panel")
    check("Left out by the marketplace exclusion" in why.inner_text(), "and the excluded count")
    table.screenshot(path=str(shots / "38-tailoring-evidence-table.png"))
    page.emulate_media(color_scheme="dark")
    page.locator("#pref-chips").screenshot(path=str(shots / "39-tailoring-chips-dark.png"))
    table.screenshot(path=str(shots / "40-tailoring-evidence-table-dark.png"))
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#pref-chips").scroll_into_view_if_needed()
    page.locator("#pref-chips").screenshot(path=str(shots / "41-tailoring-chips-mobile.png"))
    table.scroll_into_view_if_needed()
    table.screenshot(path=str(shots / "42-tailoring-evidence-table-mobile.png"))
    check(
        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
        "no sideways scroll at 390 px",
    )
    page.set_viewport_size({"width": 1280, "height": 900})
    print("deleting chips")
    leaning_chip = toward.first
    leaning_text = leaning_chip.locator(".txt").inner_text()
    leaning_chip.locator(".x").click()
    page.locator("#pref-chips .chip.pref", has_text=leaning_text).first.wait_for(
        state="detached", timeout=10000
    )
    check(True, "a leaning chip can be deleted")
    market.first.locator(".x").click()
    market.first.wait_for(state="detached", timeout=10000)
    check(market.count() == 0, "the exclusion chip can be deleted")
    page.click("#shop-again")
    wait_for_results(page)
    check(
        page.locator("#pref-chips .chip.pref.lean-filter").count() == 0
        and leaning_text not in page.inner_text("#pref-chips"),
        "neither comes back on the next Shop Again (the exclusion waits for 8 more ratings)",
    )
    after = page.locator("#grid .card .seller").all_inner_texts()
    print(f"   after deleting: {sum(1 for s in after if s in MARKETPLACE_SELLERS)} marketplace")
    context.close()


OWNER_REQUEST = "boho curtains that pair well with a dark blue room"
CREAM_WORDS = ("Cream", "Rust", "Mustard", "White", "Ivory", "Terracotta", "Ochre")


def new_page(browser: Any, base: str, width: int = 1280) -> tuple[Any, Page]:
    context = browser.new_context(viewport={"width": width, "height": 900})
    install_routes(context, 0)
    page = context.new_page()
    page.goto(base)
    page.wait_for_selector("#examples button")
    return context, page


def rate_nth(page: Page, n: int, rating: str) -> None:
    page.locator("#grid .card").nth(n).locator(f".rate.{rating}").click()


def resolved_json(link: str, status: str = "resolved") -> str:
    body = {
        "rating": {"rating": None, "rating_count": None},
        "resolved": {
            "result_id": "x", "status": status, "link": link, "link_kind": "retailer_product",
            "seller": "Mock retailer", "price": 10.0, "shipping": None,
            "shipping_status": "unknown", "total": None, "note": "", "cost_usd": 0.0,
            "resolved_at": "2026-10-26T00:00:00+00:00",
        },
        "lookups_used": 1, "lookups_cap": 12,
    }  # fmt: skip
    return json.dumps(body)


def open_button(page: Page) -> Any:
    return page.locator("#grid .card").first.locator(".open:not(.photos)")


def popup_text(popup: Page) -> str:
    try:
        return str(popup.inner_text("body"))
    except Exception:  # a page with no body yet
        return ""


def run_dry(browser: Any, bases: dict[str, str], shots: Path, console_errors: list[str]) -> None:
    """Stage 2p: a thin first search, an exhausted Shop Again, a typed colour the pool cannot
    fill, a deletable typed chip and a written refinement matched by description (mock scenario
    servers)."""
    print("stage 2p: thin results, exhaustion, colour shortfall, chip wording")

    def shoot(page: Page, name: str, locator: str) -> None:
        box = page.locator(locator).first
        box.screenshot(path=str(shots / f"{name}.png"))
        page.emulate_media(color_scheme="dark")
        box.screenshot(path=str(shots / f"{name}-dark.png"))
        page.emulate_media(color_scheme="light")
        page.set_viewport_size({"width": 390, "height": 844})
        box.scroll_into_view_if_needed()
        box.screenshot(path=str(shots / f"{name}-mobile.png"))
        check(
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
            f"no sideways scroll at 390 px ({name})",
        )
        page.set_viewport_size({"width": 1280, "height": 900})

    # a thin first search: how many matched, and the way to broaden it
    context, page = new_page(browser, bases["thin"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, "dining chairs")
    status = page.locator("#status").inner_text()
    check(
        re.search(r"Only \d+ results matched.*try broadening the request\.", status) is not None,
        f"the thin banner: {status[:110]!r}",
    )
    check(0 < page.locator("#grid .card").count() < 24, "fewer than 24 cards are shown")
    shoot(page, "80-thin-banner", "#status")
    context.close()

    # an exhausted Shop Again says so instead of showing an empty page
    context, page = new_page(browser, bases["dry"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, "dining chairs")
    rate_nth(page, 0, "love")
    page.click("#shop-again")
    page.locator("#status", has_text="I ran out of new results for this search").wait_for(
        timeout=30000
    )
    check(page.locator("#grid .card").count() == 0, "nothing is shown after the exhausted search")
    check(
        "Try changing the request or the refine chips." in page.locator("#status").inner_text(),
        "the exhaustion message names what to do",
    )
    shoot(page, "81-exhausted", "#status")
    context.close()

    # a typed colour the pool cannot fill: a banner naming it; chips that can be deleted
    context, page = new_page(browser, bases["navy"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, "navy velvet accent chairs")
    status = page.locator("#status").inner_text()
    check(
        re.search(r"Only \d+ of \d+ results match your color navy: the pool had \d+", status)
        is not None,
        f"the colour banner: {status[:120]!r}",
    )
    colour_chip = page.locator("#chips .chip.ctx", has_text="Color")
    check(
        colour_chip.count() == 1 and colour_chip.locator(".x").count() == 1,
        "the colour chip can be deleted",
    )
    shoot(page, "82-colour-shortfall", "#status")
    shoot(page, "84-typed-chips", "#chips")
    material_chip = page.locator("#chips .chip.ctx", has_text="Material")
    check(material_chip.locator(".x").count() == 1, "the material chip can be deleted")
    colour_chip.locator(".x").click()
    colour_chip.wait_for(state="detached", timeout=10000)
    check(
        page.locator("#chips .chip.ctx", has_text="Color").count() == 0,
        "deleting the chip removes it",
    )
    rate_nth(page, 0, "love")
    page.fill("#refine-input", "focus on fluted")
    page.click("#refine-add")
    page.locator("#refine-chips .chip").first.wait_for(timeout=10000)
    first = page.locator("#refine-chips .chip").first.inner_text()
    check(
        "approximate" in first.lower(),
        f"before any Shop Again the note says approximate: {first!r}",
    )
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#refine-chips .chip", has_text="matched by description").wait_for(timeout=15000)
    check(True, "after a Shop Again the written note says matched by description")
    shoot(page, "83-matched-by-description", "#refine-box")
    context.close()


def run_fixes_2q(
    browser: Any, bases: dict[str, str], shots: Path, console_errors: list[str]
) -> None:
    """Stage 2q: a short batch relaxes the per-seller cap and says so; an implausibly cheap
    furniture listing is left out and the page says so (mock `heavy` scenario)."""
    print("stage 2q: relaxed seller cap, dropped price outlier")

    def shoot(page: Page, name: str, locator: str) -> None:
        box = page.locator(locator).first
        box.screenshot(path=str(shots / f"{name}.png"))
        page.emulate_media(color_scheme="dark")
        box.screenshot(path=str(shots / f"{name}-dark.png"))
        page.emulate_media(color_scheme="light")
        page.set_viewport_size({"width": 390, "height": 844})
        box.scroll_into_view_if_needed()
        box.screenshot(path=str(shots / f"{name}-mobile.png"))
        check(
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
            f"no sideways scroll at 390 px ({name})",
        )
        page.set_viewport_size({"width": 1280, "height": 900})

    context, page = new_page(browser, bases["heavy"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, "ergonomic desk chairs")
    status = page.locator("#status").inner_text()
    check(
        "A few sellers dominate this category; allowed up to 7 per seller to fill the batch."
        in status,
        f"the relaxed-cap message: {status[:160]!r}",
    )
    check(
        "priced implausibly low for this kind of furniture" in status,
        "the dropped-price note is on the page",
    )
    check(page.locator("#grid .card").count() == 24, "the relaxed batch is full: 24 cards")
    check("$2.23" not in page.locator("#grid").inner_text(), "the $2.23 chair is not shown")
    name, share = seller_share(page)
    check(share <= 0.30, f"no seller above 30% of the batch ({name} {share:.0%})")
    shoot(page, "85-relaxed-cap", "#status")
    context.close()


def run_fixes_2r(
    browser: Any, bases: dict[str, str], shots: Path, console_errors: list[str]
) -> None:
    """Stage 2r: the fill loop's stop reason, the filtered-out breakdown, a guard switched off, and
    a typed shape quota with a deletable chip (mock scenarios `narrow`, `flat`, `roundshort`)."""
    print("stage 2r: fill stop reason, breakdown, guard off, typed shape")

    def shoot(page: Page, name: str, locator: str) -> None:
        box = page.locator(locator).first
        box.screenshot(path=str(shots / f"{name}.png"))
        page.emulate_media(color_scheme="dark")
        box.screenshot(path=str(shots / f"{name}-dark.png"))
        page.emulate_media(color_scheme="light")
        page.set_viewport_size({"width": 390, "height": 844})
        box.scroll_into_view_if_needed()
        box.screenshot(path=str(shots / f"{name}-mobile.png"))
        check(
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
            f"no sideways scroll at 390 px ({name})",
        )
        page.set_viewport_size({"width": 1280, "height": 900})

    context, page = new_page(browser, bases["narrow"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, "dining chairs under $150")
    status = page.locator("#status").inner_text()
    check(
        "outside your price" in status and "more variants (stopped:" in status,
        f"the first search says what removed results and why the fill stopped: {status[:170]!r}",
    )
    shoot(page, "86-fill-stop", "#status")
    rate_nth(page, 0, "love")
    page.click("#shop-again")
    wait_for_results(page)
    status = page.locator("#status").inner_text()
    check(
        "new results (" in status
        and "held back by the seller cap" in status
        and "more variants (stopped:" in status,
        f"a short Shop Again lists the causes and the fill stop: {status[:200]!r}",
    )
    shoot(page, "87-breakdown", "#status")
    context.close()

    context, page = new_page(browser, bases["flat"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, "area rugs")
    rate_nth(page, 0, "love")
    page.click("#shop-again")
    wait_for_results(page)
    status = page.locator("#status").inner_text()
    check(
        re.search(r"Not mixing shape: only 0 of the \d+ new candidates are not rect", status)
        is not None,
        f"the guard-off notice: {status[:160]!r}",
    )
    check("Could not keep shape" not in status, "no misleading 'could not keep' for it")
    check(page.locator("#grid .card").count() > 0, "the batch is shown")
    shoot(page, "88-guard-off", "#status")
    context.close()

    context, page = new_page(browser, bases["roundshort"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, "round black metal side tables")
    status = page.locator("#status").inner_text()
    check(
        re.search(r"Only \d+ of \d+ results match your shape round: the pool had \d+", status)
        is not None,
        f"the typed shape banner: {status[:140]!r}",
    )
    chip = page.locator("#chips .chip.ctx", has_text="Shape")
    check(chip.count() == 1 and chip.locator(".x").count() == 1, "the shape chip can be deleted")
    shoot(page, "89-typed-shape", "#status")
    chip.locator(".x").click()
    chip.wait_for(state="detached", timeout=10000)
    check(page.locator("#chips .chip.ctx", has_text="Shape").count() == 0, "deleting removes it")
    context.close()


def seed_bets(data_dir: str) -> dict[str, str]:
    """Stage 2s: two sessions of the owner's mixed case on the mock catalogue, made in-process
    (the page then opens them). `first`: the premium seller's chip was learned and the first Shop
    Again ran, so the panel shows its first-try allowance. `revoke`: the same, and the three items
    shown under the boost were then disliked, so the next Shop Again revokes it."""
    from app.config import Config
    from app.shop.providers import PREMIUM_SELLER, build_shop_providers
    from app.shop.service import ShopService

    cfg = Config.from_env()
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(Path(data_dir)), search, lookup)

    def pick(rows: list[dict[str, Any]], *needles: str, premium: bool = True) -> str:
        for r in rows:
            if (r["seller"] == PREMIUM_SELLER) == premium and all(n in r["title"] for n in needles):
                return str(r["id"])
        raise RuntimeError(f"no result with {needles}")

    out: dict[str, str] = {}
    for name in ("first", "revoke"):
        view = svc.search(None, COFFEE)
        sid, rows = view["session_id"], view["search"]["results"]
        for needles, premium, rating in (
            (("Round Walnut Drum",), True, "love"),
            (("Rectangular Wood and Black Metal",), True, "love"),
            (("Round Brushed Brass Drum",), True, "love"),
            (("Rectangular Walnut",), True, "love"),
            (("Round ",), False, "like"),
            (("Rectangular ",), False, "like"),
            (("Oval ",), False, "dislike"),
        ):
            svc.rate(sid, pick(rows, *needles, premium=premium), rating)
        rated = set(svc.store.load(sid).ratings)
        others = [r["id"] for r in rows if r["seller"] != PREMIUM_SELLER and r["id"] not in rated]
        for rid in others[:6]:  # a low baseline: the premium seller's 4 of 4 is then 1.5x it
            svc.rate(sid, rid, "dislike")
        shown = svc.shop_again(sid)["search"]["results"]
        if name == "revoke":
            mine = [
                r["id"] for r in shown
                if r["seller"] == PREMIUM_SELLER and (r.get("source") or {}).get("kind") == "seller"
            ]  # fmt: skip
            for rid in mine[:4]:
                svc.rate(sid, rid, "dislike")
        out[name] = sid
    return out


def run_seller_bets(
    browser: Any, base: str, ids: dict[str, str], shots: Path, console_errors: list[str]
) -> None:
    """Stage 2s: the first-try allowance in "How I decided", and the revocation message."""
    print("stage 2s: first-try allowance, revoked seller bet")

    def shoot(page: Page, name: str, locator: str) -> None:
        box = page.locator(locator).first
        box.screenshot(path=str(shots / f"{name}.png"))
        page.emulate_media(color_scheme="dark")
        box.screenshot(path=str(shots / f"{name}-dark.png"))
        page.emulate_media(color_scheme="light")
        page.set_viewport_size({"width": 390, "height": 844})
        box.scroll_into_view_if_needed()
        box.screenshot(path=str(shots / f"{name}-mobile.png"))
        check(
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
            f"no sideways scroll at 390 px ({name})",
        )
        page.set_viewport_size({"width": 1280, "height": 900})

    context, page = open_session(browser, base, ids["first"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.click("#shop-again")  # the boosted items are still unrated: the bet stays a first try
    wait_for_results(page)
    page.locator("#why-toggle").click()
    line = page.locator("#why .why-line", has_text="slots, first try")
    check(line.count() >= 1, "How I decided names the seller's allowance: 'N slots, first try'")
    check(
        "Hartwell & Co: 4 slots, first try" in page.locator("#why").inner_text(),
        "the allowance reads '4 slots, first try'",
    )
    check("exposure" in page.locator("#why .why-evidence").inner_text().lower(),
          "the evidence table has an exposure column")  # fmt: skip
    shoot(page, "90-first-try", "#why .why-line:has-text('first try')")
    context.close()

    context, page = open_session(browser, base, ids["revoke"])
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.click("#shop-again")
    wait_for_results(page)
    status = page.locator("#status").inner_text()
    check(
        "Tried more Hartwell & Co; you did not like most of it, so I stopped." in status,
        f"the revocation message: {status[:150]!r}",
    )
    check(
        page.locator("#pref-chips .chip.pref", has_text="seller: Hartwell").count() == 0,
        "the seller chip is gone",
    )
    shoot(page, "91-revoked", "#status")
    context.close()


def run_local_unchanged(browser: Any, base: str, shots: Path, console_errors: list[str]) -> None:
    """Stage D1: with no deployment settings the local page is what it was: the home view, a results
    view and the Saved panel, no login, the old `/api` routes; the deployed prefix and /healthz are
    404 here."""
    print("stage D1: the local page is unchanged (home, results, Saved)")

    def shoot(page: Page, name: str, locator: str | None = None) -> None:
        def take(suffix: str) -> None:
            path = str(shots / f"{name}{suffix}.png")
            if locator:
                page.locator(locator).first.screenshot(path=path)
            else:
                page.screenshot(path=path)

        take("")
        page.emulate_media(color_scheme="dark")
        take("-dark")
        page.emulate_media(color_scheme="light")
        page.set_viewport_size({"width": 390, "height": 844})
        take("-mobile")
        check(
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
            f"no sideways scroll at 390 px ({name})",
        )
        page.set_viewport_size({"width": 1280, "height": 900})

    context, page = new_page(browser, base)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    check(page.locator("#search-form").is_visible(), "the home view shows the search form")
    check(page.locator("#examples button, #examples a").count() >= 1, "the examples are listed")
    check(
        page.evaluate(
            "async () => { const r = await fetch('/api/auth/session');"
            " return (await r.json()).local === true; }"
        ),
        "locally /api/auth/session says local, no login",
    )
    check(
        page.evaluate(
            "async () => [(await fetch('/api/shop/session')).status,"
            " (await fetch('/healthz')).status]"
        )
        == [404, 404],
        "the deployment prefix and /healthz are not served locally",
    )
    shoot(page, "92-local-home")
    page.fill("#request", COFFEE)
    page.click("#search-btn")
    page.locator(".card").first.wait_for(timeout=15000)
    page.locator(".skeleton").first.wait_for(state="detached", timeout=15000)
    check(page.locator("#grid .card").count() > 0, "a search shows a results grid with no login")
    shoot(page, "93-local-results")
    rate_nth(page, 0, "love")
    page.locator("#saved-toggle").click()
    page.locator("#saved").wait_for(state="visible")
    check(
        page.locator("#saved-row .card, #saved-row *").count() >= 1,
        "the Saved panel shows the loved item",
    )
    shoot(page, "94-local-saved", "#saved")
    context.close()


def run_open(browser: Any, base: str, shots: Path, console_errors: list[str]) -> None:
    """Stage 2o: Open never leaves a blank tab. The store lookup is held, failed or answered by
    this script (the page's /api/resolve), so each case is deterministic."""
    print("stage 2o: Open success, failure, timeout and a blocked popup")
    shop = "https://shop.example.test/product/1"

    def fresh() -> tuple[Any, Page]:
        context, page = new_page(browser, base)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        search(page, COFFEE)
        return context, page

    # success: a loading message while the lookup is held, then the store page
    context, page = fresh()
    check(
        page.locator("#grid .photos").count() == 0, "no More photos button while the gallery is off"
    )
    check(
        page.locator("#grid .gallery").count() == 0, "no gallery element while the gallery is off"
    )
    held: list[Route] = []
    page.route("**/api/resolve", lambda route: held.append(route))
    with context.expect_page() as info:
        open_button(page).click()
    popup = info.value
    page.wait_for_function(
        "el => el.disabled", arg=open_button(page).element_handle(), timeout=5000
    )
    check(open_button(page).inner_text() == "Finding store…", "the button reads Finding store...")
    check("Finding the store page" in popup_text(popup), "the new tab says what it is doing")
    check(
        popup.url != "about:blank" or bool(popup_text(popup)), "the tab is not blank while waiting"
    )
    open_button(page).click(force=True)  # a second click reuses the one lookup in flight
    check(len(held) == 1, f"one lookup in flight after a second click ({len(held)})")
    held[0].fulfill(status=200, content_type="application/json", body=resolved_json(shop))
    popup.wait_for_url(shop, timeout=10000)
    check(
        popup.url == shop and "Mock retailer" in popup_text(popup), "the tab went to the store page"
    )
    check(
        not open_button(page).is_disabled() and open_button(page).inner_text() == "Open",
        "button back",
    )
    context.close()

    # failure: the Google product page and one line on the card
    context, page = fresh()
    page.route(
        "**/api/resolve",
        lambda route: route.fulfill(
            status=502, content_type="application/json", body='{"error": "lookup failed"}'
        ),
    )
    with context.expect_page() as info:
        open_button(page).click()
    popup = info.value
    popup.wait_for_url(re.compile(r"https://www\.google\.com/"), timeout=10000)
    check(popup.url != "about:blank", "a failed lookup does not leave a blank tab")
    note = page.locator("#grid .card").first.locator(".open-note")
    note.wait_for(state="visible", timeout=5000)
    check(
        note.inner_text() == "Could not find the store page; showing Google's product page.",
        f"the card says so: {note.inner_text()!r}",
    )
    card = page.locator("#grid .card").first
    card.screenshot(path=str(shots / "70-open-fallback.png"))
    page.emulate_media(color_scheme="dark")
    card.screenshot(path=str(shots / "71-open-fallback-dark.png"))
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    card.scroll_into_view_if_needed()
    card.screenshot(path=str(shots / "72-open-fallback-mobile.png"))
    check(
        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
        "no sideways scroll at 390 px (open message)",
    )
    context.close()

    # timeout: the lookup never answers; after 12 s the Google page opens
    context, page = fresh()
    stuck: list[Route] = []
    page.route("**/api/resolve", lambda route: stuck.append(route))
    with context.expect_page() as info:
        open_button(page).click()
    popup = info.value
    popup.wait_for_url(re.compile(r"https://www\.google\.com/"), timeout=20000)
    check(popup.url != "about:blank", "a lookup that times out does not leave a blank tab")
    check(
        "Could not find the store page" in page.locator("#grid .card").first.inner_text(),
        "the card explains the timeout",
    )
    for route in stuck:  # the lookup that never answered is dropped before the page closes
        route.abort()
    context.close()

    # a blocked popup: a message, then one inline link when the store is found
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    install_routes(context, 0)
    context.add_init_script("window.open = function () { return null; };")
    page = context.new_page()
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.goto(base)
    page.wait_for_selector("#examples button")
    search(page, COFFEE)
    page.route(
        "**/api/resolve",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=resolved_json(shop)
        ),
    )
    open_button(page).click()
    card = page.locator("#grid .card").first
    link = card.locator(".open-note a")
    link.wait_for(state="visible", timeout=5000)
    check(link.inner_text() == "Go to store", "a blocked tab leaves a Go to store link")
    check(
        link.get_attribute("href") == shop and "noopener" in (link.get_attribute("rel") or ""),
        "the link is the store page with rel noopener",
    )
    check(
        "blocked" in card.locator(".open-note").inner_text().lower(),
        "the card says the tab was blocked",
    )
    card.screenshot(path=str(shots / "73-open-blocked.png"))
    page.emulate_media(color_scheme="dark")
    card.screenshot(path=str(shots / "74-open-blocked-dark.png"))
    page.emulate_media(color_scheme="light")
    context.close()


def run_fixes(browser: Any, base: str, shots: Path, console_errors: list[str]) -> None:
    """Stage 2n on a server with mock same-product vendors and one-photo products."""
    print("stage 2n: duplicate vendors, one-photo products, meta comments, small wording")
    context, page = new_page(browser, base)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, COFFEE)
    lines = page.locator("#grid .card .also-at")
    check(
        lines.count() >= 1,
        f"{lines.count()} cards carry an Also at line (one product, many vendors)",
    )
    text = lines.first.inner_text()
    check(
        re.match(r"Also at: .+ \$[\d,]+\.\d\d", text) is not None and "http" not in text,
        f"the line is text with seller and price: {text!r}",
    )
    check(
        lines.first.locator("a").count() == 0,
        "the Also at line has no links",
    )
    titles = title_texts(page)
    check(len(titles) == len(set(titles)), "no title is shown twice in the batch")
    card = lines.first.locator("xpath=ancestor::*[contains(@class,'card')][1]")
    card.screenshot(path=str(shots / "60-also-at.png"))
    page.emulate_media(color_scheme="dark")
    card.screenshot(path=str(shots / "61-also-at-dark.png"))
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    card.scroll_into_view_if_needed()
    card.screenshot(path=str(shots / "62-also-at-mobile.png"))
    box = page.locator("#refine-input")
    check(box.get_attribute("placeholder") == "e.g. no Walmart", "the refine placeholder is short")
    check(
        "focus on cream" in (box.get_attribute("title") or "")
        and "focus on cream" in (box.get_attribute("aria-label") or ""),
        "the full examples stay in the title and the accessible label",
    )
    page.locator("#refine-form").screenshot(path=str(shots / "63-refine-mobile.png"))
    check(
        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
        "no sideways scroll at 390 px (duplicates)",
    )
    page.set_viewport_size({"width": 1280, "height": 900})

    # a product with one photo has no gallery
    first = page.locator("#grid .card").first
    button = first.locator(".photos")
    button.click()
    page.wait_for_function(
        "el => el.textContent === 'No more photos available'",
        arg=button.element_handle(),
        timeout=10000,
    )
    check(button.is_disabled(), "No more photos available is a disabled button")
    check(
        first.locator(".gallery").count() == 0 or first.locator(".gallery").is_hidden(),
        "no gallery",
    )
    check("link lookups 1 of 12" in lookups_text(page), "the footer counts that lookup once")
    first.screenshot(path=str(shots / "64-no-more-photos.png"))

    # a comment about the listing makes no chip
    for n, kind in enumerate(("love", "love", "like")):
        rate_nth(page, n + 1, kind)
    flagged = page.locator("#grid .card").nth(4)
    flagged.locator(".rate.dislike").click()
    note = flagged.locator(".note")
    note.fill("duplicate")
    note.press("Tab")
    page.wait_for_load_state("networkidle")
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#prefs").wait_for(state="visible")
    chip_text = page.locator("#pref-chips").inner_text().lower()
    check("duplicate" not in chip_text, f"no chip from the comment 'duplicate': {chip_text[:80]!r}")
    page.click("#why-toggle")
    why = page.locator("#why")
    why.wait_for(state="visible")
    why_text = why.inner_text()
    check("flagged duplicate by you" in why_text, "How I decided says the item was flagged")
    check("Comments about the listing, not your taste" in why_text, "the meta items have a heading")
    check(
        "would have caught it" in why_text or "would not have caught it" in why_text,
        "it says whether the automatic check would have caught it",
    )
    foot = lookups_text(page)
    check("matches your preferences" in foot, f"the footer wording: {foot[:120]!r}")
    why.screenshot(path=str(shots / "65-meta-comment-trace.png"))
    page.emulate_media(color_scheme="dark")
    why.screenshot(path=str(shots / "66-meta-comment-trace-dark.png"))
    page.emulate_media(color_scheme="light")
    context.close()


def run_batch(
    browser: Any,
    base: str,
    base_ev: str,
    id_ev: str,
    shots: Path,
    console_errors: list[str],
) -> None:
    print("topics: coffee tables with ratings, then couches")
    context, page = new_page(browser, base)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, COFFEE)
    for n, kind in enumerate(("love", "love", "like", "dislike")):
        rate_nth(page, n, kind)
    page.wait_for_load_state("networkidle")
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#prefs").wait_for(state="visible")
    check(
        page.locator("#pref-chips .chip.pref").count() >= 1, "chips learned from the coffee tables"
    )
    page.fill("#request", "modern couches under $1000")
    page.click("#search-btn")
    wait_for_results(page)
    notice = page.locator("#topic-notice")
    check(notice.is_visible(), "the topic notice is shown")
    check(
        notice.inner_text().startswith(
            "Started fresh for couches. Your saved coffee tables are still in Saved"
        ),
        f"the notice says so: {notice.inner_text()[:90]!r}",
    )
    check(page.locator("#pref-chips .chip.pref").count() == 0, "the learned chips are cleared")
    check(
        page.locator("#shop-again").is_disabled(), "Shop Again waits for ratings in the new topic"
    )
    page.locator(".top").screenshot(path=str(shots / "43-topic-notice.png"))
    page.click("#saved-toggle")
    page.locator("#saved .card .topic-tag").first.wait_for(timeout=10000)
    check(
        "From an earlier search" in page.locator("#saved .card .topic-tag").first.inner_text(),
        "saved items from the earlier topic are labelled",
    )
    page.locator("#saved").screenshot(path=str(shots / "44-saved-earlier-topic.png"))
    page.emulate_media(color_scheme="dark")
    page.locator(".top").screenshot(path=str(shots / "45-topic-notice-dark.png"))
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator(".top").screenshot(path=str(shots / "46-topic-notice-mobile.png"))
    check(
        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
        "no sideways scroll at 390 px (topics)",
    )
    page.set_viewport_size({"width": 1280, "height": 900})
    context.close()

    print("the owner's curtains request: setting, suggestions, stars and the gallery")
    context, page = new_page(browser, base)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    search(page, OWNER_REQUEST)
    chips_text = page.inner_text("#chips").lower()
    check(
        "search" in chips_text and "boho curtains" in chips_text,
        "the search chip is 'boho curtains'",
    )
    check(
        page.locator("#chips .chip.ctx", has_text="dark blue").count() == 1
        and "room" in page.locator("#chips .chip.ctx").first.inner_text().lower(),
        "the room colour is a setting chip",
    )
    suggested = page.locator("#chips .chip.ctx.suggested")
    avoid = page.locator("#chips .chip.ctx.avoidc")
    check(
        suggested.count() == 4 and avoid.count() == 2,
        f"{suggested.count()} suggested and {avoid.count()} avoid chips",
    )
    check(
        "dark blue"
        not in page.locator("#chips .chip:not(.ctx)").all_inner_texts().__str__().lower(),
        "no product chip says dark blue",
    )
    page.locator("#request-trace summary").click()
    check(
        "only what describes the product" in page.inner_text("#request-trace-body"),
        "the request trace explains the split",
    )
    titles = title_texts(page)
    warm = sum(1 for t in titles if any(w.lower() in t.lower() for w in CREAM_WORDS))
    navy = sum(1 for t in titles if any(w in t for w in ("Navy", "Dark Blue", "Blue")))
    check(
        warm >= 8
        and navy <= 3,  # stage 2p: photo tags (mock: pseudo photos) pick some untitled colours
        f"{warm} of {len(titles)} in suggested colours, {navy} in colours to avoid",
    )
    stars = page.locator("#grid .card .rating-row .stars")
    check(stars.count() >= 10, f"{stars.count()} cards show stars")
    label = stars.first.get_attribute("aria-label") or ""
    check(
        re.match(r"rated \d\.\d out of 5( from [\d,]+ reviews)?$", label) is not None,
        f"accessible text: {label!r}",
    )
    page.locator(".top").screenshot(path=str(shots / "47-curtains-setting-chips.png"))
    page.locator("#grid .card").first.screenshot(path=str(shots / "48-card-with-stars.png"))
    # delete one suggestion and the avoid colour
    count = suggested.count()
    suggested.first.locator(".x").click()
    page.locator("#chips .chip.ctx.suggested").nth(count - 1).wait_for(
        state="detached", timeout=10000
    )
    check(
        page.locator("#chips .chip.ctx.suggested").count() == count - 1,
        "a suggested colour can be deleted",
    )
    # the gallery
    first_card = page.locator("#grid .card").first
    button = first_card.locator(".photos")
    check(
        button.count() == 1 and button.inner_text() == "More photos",
        "a card with a product id has More photos",
    )
    button.click()
    gallery = first_card.locator(".gallery")
    gallery.wait_for(state="visible", timeout=10000)
    check(first_card.locator(".g-count").inner_text() == "1 of 6", "the gallery has six photos")
    first_card.locator(".g-nav").nth(1).click()
    check(first_card.locator(".g-count").inner_text() == "2 of 6", "next shows the second photo")
    gallery.focus()
    page.keyboard.press("ArrowRight")
    check(
        first_card.locator(".g-count").inner_text() == "3 of 6",
        "the arrow keys move through the photos",
    )
    page.keyboard.press("ArrowLeft")
    check(first_card.locator(".g-count").inner_text() == "2 of 6", "and back")
    check(
        button.get_attribute("aria-expanded") == "true" and button.inner_text() == "Hide photos",
        "the button toggles",
    )
    check("link lookups 1 of 12" in lookups_text(page), "the footer counts the lookup")
    first_card.screenshot(path=str(shots / "49-gallery.png"))
    page.emulate_media(color_scheme="dark")
    first_card.screenshot(path=str(shots / "50-gallery-dark.png"))
    page.locator(".top").screenshot(path=str(shots / "51-curtains-setting-chips-dark.png"))
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    first_card.scroll_into_view_if_needed()
    first_card.screenshot(path=str(shots / "52-gallery-mobile.png"))
    page.locator(".top").screenshot(path=str(shots / "53-curtains-setting-chips-mobile.png"))
    check(
        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
        "no sideways scroll at 390 px (curtains)",
    )
    page.set_viewport_size({"width": 1280, "height": 900})
    button.click()
    check(first_card.locator(".gallery").is_hidden(), "Hide photos closes the gallery")
    context.close()

    print("refine: typed instructions become chips")
    context, page = open_session(browser, base_ev, id_ev)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    box = page.locator("#refine-input")
    check(box.get_attribute("maxlength") == "200", "the refine box takes 200 characters")
    box.fill("no target")
    page.click("#refine-add")
    page.locator("#refine-chips .chip.askchip").first.wait_for(timeout=10000)
    check(box.input_value() == "", "the box empties after a submission")
    chip = page.locator("#refine-chips .chip.askchip").first
    check(
        "you asked" in chip.inner_text().lower() and "no target" in chip.inner_text().lower(),
        "a 'you asked' chip appears",
    )
    box.fill("focus on metal frames")
    box.press("Enter")
    page.locator("#refine-chips .chip.askchip").nth(1).wait_for(timeout=10000)
    box.fill("asdf qwer zxcv")
    page.click("#refine-add")
    page.locator("#refine-note.error").wait_for(timeout=10000)
    check(
        "could not turn that" in page.inner_text("#refine-note"),
        "nonsense is rejected with a short message",
    )
    box.fill("under $400")  # left in the box: read as a step of the next Shop Again
    page.click("#shop-again")
    wait_for_results(page)
    page.locator("#refine-chips .chip.askchip").nth(2).wait_for(timeout=10000)
    check(
        box.input_value() == "" and page.locator("#refine-chips .chip.askchip").count() == 3,
        "text left in the box became a chip",
    )
    sellers = page.locator("#grid .card .seller").all_inner_texts()
    check("Target" not in sellers, "nothing from the excluded seller is in the batch")
    prices = [
        price_of(page.locator("#grid .card").nth(i))
        for i in range(page.locator("#grid .card").count())
    ]
    check(
        bool(prices) and max(prices) <= 400,
        f"every price is under $400 (max {max(prices) if prices else 0:.0f})",
    )
    check(
        "under $400" in page.inner_text("#chips") or "up to $400" in page.inner_text("#chips"),
        "the price chip changed",
    )
    page.locator("#why-toggle").click()
    check(
        "You asked: no target" in page.locator("#why").inner_text().lower()
        or "you asked: no target" in page.locator("#why").inner_text().lower(),
        "the trace lists the refine chip",
    )
    page.locator("#refine-box").screenshot(path=str(shots / "54-refine-chips.png"))
    page.locator(".actions").screenshot(path=str(shots / "55-refine-box.png"))
    page.emulate_media(color_scheme="dark")
    page.locator("#refine-box").screenshot(path=str(shots / "56-refine-chips-dark.png"))
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator(".actions").scroll_into_view_if_needed()
    page.locator(".toolbar").screenshot(path=str(shots / "57-refine-mobile.png"))
    check(
        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
        "no sideways scroll at 390 px (refine)",
    )
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator("#refine-chips .chip.askchip").first.locator(".x").click()
    page.locator("#refine-chips .chip.askchip").nth(2).wait_for(state="detached", timeout=10000)
    check(page.locator("#refine-chips .chip.askchip").count() == 2, "a refine chip can be deleted")
    context.close()


def _clip(page: Page, chip: Any) -> dict[str, float]:
    box = chip.bounding_box() or {"x": 0, "y": 0, "width": 200, "height": 30}
    top = max(0.0, box["y"] - 10)
    return {"x": 0, "y": top, "width": 1280, "height": min(900 - top, 260)}


def start_server(data_dir: str, extra_env: dict[str, str] | None = None) -> tuple[Any, int]:
    """Start `scripts/shop.py` on a free port in mock mode; returns (process, port)."""
    port = free_port()
    env: dict[str, Any] = {
        **os.environ, "APP_LLM_MODE": "mock", "APP_TRACING_DISABLED": "1", **(extra_env or {}),
    }  # fmt: skip
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "shop.py"), "--port", str(port),
         "--data-dir", data_dir],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    assert server.stdout is not None
    first = server.stdout.readline()  # printed once the socket is bound
    print(first.strip())
    if "127.0.0.1" not in first:
        server.kill()
        raise RuntimeError("the server did not start")
    return server, port


def stop_server(server: Any) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    servers: list[Any] = []
    with (
        tempfile.TemporaryDirectory() as data_dir,
        tempfile.TemporaryDirectory() as data_dir3,
        tempfile.TemporaryDirectory() as data_dir4,
        tempfile.TemporaryDirectory() as data_dir5,
        tempfile.TemporaryDirectory() as data_dir6,
        tempfile.TemporaryDirectory() as data_dir7,
        tempfile.TemporaryDirectory() as data_dir8,
        tempfile.TemporaryDirectory() as dir_thin,
        tempfile.TemporaryDirectory() as dir_dry,
        tempfile.TemporaryDirectory() as dir_navy,
        tempfile.TemporaryDirectory() as dir_heavy,
        tempfile.TemporaryDirectory() as dir_narrow,
        tempfile.TemporaryDirectory() as dir_flat,
        tempfile.TemporaryDirectory() as dir_roundshort,
        tempfile.TemporaryDirectory() as dir_bets,
    ):
        try:
            first, port = start_server(data_dir, {"APP_SHOP_GALLERY": "1"})
            servers.append(first)
            # the third replay's server serves its authored candidate pool (mock only)
            third, port3 = start_server(data_dir3, {"APP_SHOP_MOCK_POOL": REPLAY_FILE_3})
            servers.append(third)
            fourth, port4 = start_server(data_dir4, {"APP_SHOP_MOCK_POOL": REPLAY_FILE_4})
            servers.append(fourth)
            fifth, port5 = start_server(
                data_dir5, {"APP_SHOP_MOCK_POOL": REPLAY_FILE_4, "APP_SHOP_MOCK_DELAY": "1.2"}
            )
            servers.append(fifth)
            sixth, port6 = start_server(data_dir6, {"APP_SHOP_MOCK_POOL": REPLAY_FILE_EVIDENCE})
            servers.append(sixth)
            seventh, port7 = start_server(
                data_dir7,
                {
                    "APP_SHOP_MOCK_DUPLICATES": "1",
                    "APP_SHOP_MOCK_ONE_PHOTO": "1",
                    "APP_SHOP_GALLERY": "1",
                },
            )
            servers.append(seventh)
            eighth, port8 = start_server(data_dir8)
            servers.append(eighth)
            scenario_dirs = {
                "thin": dir_thin,
                "dry": dir_dry,
                "navy": dir_navy,
                "heavy": dir_heavy,
                "narrow": dir_narrow,
                "flat": dir_flat,
                "roundshort": dir_roundshort,
            }
            scenario_ports: dict[str, str] = {}
            for name, extra in (
                ("thin", {"APP_SHOP_MODEL_QUERIES": "0"}),
                ("dry", {"APP_SHOP_MODEL_QUERIES": "0", "APP_SHOP_THIN_VARIANTS": "0"}),
                ("navy", {}),
                ("heavy", {"APP_SHOP_MODEL_QUERIES": "0", "APP_SHOP_THIN_VARIANTS": "0"}),
                ("narrow", {"APP_SHOP_MODEL_QUERIES": "0"}),
                ("flat", {"APP_SHOP_MODEL_QUERIES": "0"}),
                ("roundshort", {"APP_SHOP_MODEL_QUERIES": "0"}),
            ):
                server_n, port_n = start_server(
                    scenario_dirs[name], {"APP_SHOP_MOCK_SCENARIO": name, **extra}
                )
                servers.append(server_n)
                scenario_ports[name] = f"http://127.0.0.1:{port_n}/"
            BET_IDS.update(seed_bets(dir_bets))
            bets_server, bets_port = start_server(dir_bets)
            servers.append(bets_server)
            scenario_ports["bets"] = f"http://127.0.0.1:{bets_port}/"
            tailoring = seed_session(
                SessionStore(Path(data_dir6)), replay=REPLAY_FILE_EVIDENCE
            ).session_id
            second_evidence = seed_session(
                SessionStore(Path(data_dir6)), replay=REPLAY_FILE_EVIDENCE
            ).session_id
            run_checks(
                f"http://127.0.0.1:{port}/", OUT, seed_replays(data_dir),
                f"http://127.0.0.1:{port3}/", seed_replays_3(data_dir3),
                f"http://127.0.0.1:{port4}/", seed_replays_4(data_dir4),
                f"http://127.0.0.1:{port5}/", seed_replays_4(data_dir5),
                f"http://127.0.0.1:{port6}/", tailoring, second_evidence,
                f"http://127.0.0.1:{port7}/",
                f"http://127.0.0.1:{port8}/", scenario_ports,
            )  # fmt: skip
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            for server in servers:
                stop_server(server)
            print("servers stopped")
    print(f"screenshots: {OUT}")
    if FAILS:
        print(f"{len(FAILS)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
