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
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Route, sync_playwright

from app.shop.replay import REPLAY_FILE_2, seed_session
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


def seed_replays(data_dir: str) -> dict[str, str]:
    """Put the owner's recorded session (and a copy whose preference call is forced to fail) in the
    server's session folder; the page opens them by id."""
    store = SessionStore(Path(data_dir))
    clean = seed_session(store)
    broken = seed_session(
        store,
        comments={"R1": "MOCK_TRUNCATE_ALL"},
        extra_ratings={"X20": ("dislike", "MOCK_TRUNCATE_ALL")},
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
        seller_chip.count() == 1,
        "the West Elm seller chip appears (the lift test passes at 50% exposure)",
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
    check(any(n.startswith("seller: chip") for n in names), "the seller decision is listed")
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
        "tagged 0 of 11 items" in lookups_text(page) and "fallback" in lookups_text(page),
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
    check("like share 25% is under 40%" in text, "the Home Depot rule that failed is shown")
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
        "could not be tagged" in page.inner_text("#status"),
        "the page says the results could not be tagged",
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


def run_checks(base: str, shots: Path, replay_ids: dict[str, str]) -> None:
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
            page.locator("#spinner").wait_for(state="visible")
            check(
                page.inner_text("#status") == "Searching, usually about 10 seconds", "loading text"
            )
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
                "the premium seller chip (2 loved items, over 40% of the like mass)",
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
            check(
                share2 > 0.5 and share2 <= 0.8,
                "a pinned seller may fill more than half, never more than 80%",
            )
            check(
                chips(page) == request_chips,
                "the request chips are unchanged after the second Shop Again",
            )
            page.screenshot(path=str(shots / "07-scenario-a-grid-after.png"))

            print("Saved panel wraps")
            page.click("#saved-toggle")
            check(page.locator("#saved").is_visible(), "Saved opens")
            check(saved_counts(page)[0].startswith("Loved (2)"), f"tabs {saved_counts(page)}")
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
            check(console_errors == [], f"no console or page errors {console_errors[:3]}")
        finally:
            browser.close()


def _clip(page: Page, chip: Any) -> dict[str, float]:
    box = chip.bounding_box() or {"x": 0, "y": 0, "width": 200, "height": 30}
    top = max(0.0, box["y"] - 10)
    return {"x": 0, "y": top, "width": 1280, "height": min(900 - top, 260)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    port = free_port()
    env: dict[str, Any] = {**os.environ, "APP_LLM_MODE": "mock", "APP_TRACING_DISABLED": "1"}
    with tempfile.TemporaryDirectory() as data_dir:
        server = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "shop.py"), "--port", str(port),
             "--data-dir", data_dir],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        try:
            assert server.stdout is not None
            first = server.stdout.readline()  # printed once the socket is bound
            print(first.strip())
            if "127.0.0.1" not in first:
                print("ERROR: the server did not start", file=sys.stderr)
                return 1
            run_checks(f"http://127.0.0.1:{port}/", OUT, seed_replays(data_dir))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            print("server stopped")
    print(f"screenshots: {OUT}")
    if FAILS:
        print(f"{len(FAILS)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
