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


def run_checks(base: str, shots: Path) -> None:
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
            check(page.locator(".card").count() == 0, "no cards before a search")
            check(
                page.locator("#shop-again").is_disabled(), "Shop Again is disabled before a rating"
            )
            check(
                page.locator("#again-wrap").get_attribute("title") == "Rate a few results first",
                "tooltip says Rate a few results first",
            )
            page.screenshot(path=str(shots / "01-empty.png"))

            print("a failed search shows a readable error and a retry")
            page.route(
                "**/api/search",
                lambda r: r.fulfill(
                    status=502, content_type="application/json",
                    body='{"error": "serper: HTTP 402 payment required"}',
                ),
            )  # fmt: skip
            page.fill("#request", COFFEE)
            page.click("#search-btn")
            page.locator("#status.error").wait_for()
            check("serper: HTTP 402" in page.inner_text("#status"), "error message is shown")
            check(page.locator("#retry").is_visible(), "a Retry button is shown")
            page.screenshot(path=str(shots / "02-error-retry.png"))
            page.unroute("**/api/search")

            print("search, with a visible loading state")
            release = hold(page, "**/api/search")
            page.click("#retry")
            page.locator("#spinner").wait_for(state="visible")
            check(
                page.inner_text("#status") == "Searching, usually about 10 seconds",
                "loading text says Searching, usually about 10 seconds",
            )
            check(page.locator("#search-btn").is_disabled(), "Search is disabled while loading")
            page.screenshot(path=str(shots / "03-loading.png"))
            release()
            wait_for_results(page)
            first_ids = card_ids(page)
            check(len(first_ids) == 24, f"{len(first_ids)} results in the first batch")
            first_premium = count_seller(page, PREMIUM)
            print(f"   {first_premium} of the first batch are from {PREMIUM}")
            check(
                first_premium >= 2, "the first batch has two or more items from the invented seller"
            )
            check(
                any("industrial" in c for c in chips(page)),
                "the parser's style guess shows in the request chips",
            )
            page.screenshot(path=str(shots / "04-first-batch.png"))

            print("love two items from one seller (no comments), like two others, dislike one")
            brass = seller_cards(page, PREMIUM).filter(
                has=page.locator(".title", has_text="Brushed Brass")
            )
            check(brass.count() >= 2, "two brushed brass items from the invented seller")
            brass.nth(0).locator(".rate.love").click()
            brass.nth(1).locator(".rate.love").click()
            others = (
                page.locator("#grid .card")
                .filter(has_not=page.locator(".seller", has_text=PREMIUM))
                .filter(has_not=page.locator(".title", has_text="Industrial"))
            )
            others.nth(0).locator(".rate.like").click()
            others.nth(1).locator(".rate.like").click()
            others.nth(2).locator(".rate.dislike").click()
            page.wait_for_load_state("networkidle")
            check("link lookups 0 of 12" in lookups_text(page), "loving and liking ran no lookup")
            check(
                brass.nth(0).locator(".resolved").inner_text() == "",
                "a loved card keeps its Google link (nothing resolved)",
            )
            check(
                page.inner_text("#saved-toggle") == "Show saved (4)",
                "Saved holds the 4 loved and liked",
            )

            print("Shop Again")
            release = hold(page, "**/api/shop-again")
            page.click("#shop-again")
            page.locator("#spinner").wait_for(state="visible")
            check(
                page.inner_text("#status") == "Finding new results, usually about 10 seconds",
                "Shop Again loading text",
            )
            release()
            wait_for_results(page)
            page.locator("#prefs").wait_for(state="visible")
            new_ids = card_ids(page)
            check(len(new_ids) == 24, f"{len(new_ids)} new results")
            check(
                not set(new_ids) & set(first_ids), "no result from the first batch is shown again"
            )
            again_premium = count_seller(page, PREMIUM)
            print(f"   {again_premium} of the Shop Again batch are from {PREMIUM}")
            check(
                again_premium > first_premium, "more results from the favoured seller than before"
            )
            print("   summary:", page.inner_text("#prefs-summary"))
            print("   footer:", lookups_text(page))
            check(PREMIUM in page.inner_text("#prefs-summary"), "the summary names the seller")
            inferred = page.locator("#pref-chips .chip.pref.inferred")
            check(inferred.count() >= 2, f"{inferred.count()} inferred chips")
            chip_text = page.inner_text("#pref-chips")
            check("brushed brass" in chip_text, "an inferred 'brushed brass' chip")
            check(f"seller: {PREMIUM}" in chip_text, "an inferred seller chip")
            tag_text = page.locator("#pref-chips .chip.pref.inferred .tag").first.text_content()
            check(tag_text == "inferred", f"inferred chips are tagged ({tag_text!r})")
            check(
                page.locator("#pref-chips .chip.pref.guess").count() == 0
                and "industrial" not in chip_text,
                "the guess was dropped: loved items are not industrial",
            )
            check("matches your preferences:" in lookups_text(page), "footer shows the alignment")
            check("3 searches" in lookups_text(page), "footer shows three search phrasings")
            check("link lookups 0 of 12" in lookups_text(page), "Shop Again ran no lookup")
            page.screenshot(path=str(shots / "05-chips-after-shop-again.png"))

            print("evidence tooltip")
            chip = page.locator("#pref-chips .chip.pref.inferred").filter(
                has=page.locator(".txt", has_text=re.compile(r"^brushed brass$"))
            )
            tip = chip.locator(".tip")
            check(tip.is_hidden(), "the tooltip is hidden until hover")
            chip.hover()
            check(tip.is_visible(), "the tooltip shows on hover")
            check("Brushed Brass" in tip.inner_text(), "the tooltip lists the evidence titles")
            check(tip.inner_text().startswith("Learned from what you rated"), "the tooltip heading")
            page.screenshot(path=str(shots / "06-tooltip.png"), clip=_clip(page, chip))
            page.mouse.move(5, 400)

            print("styles of the three kinds of chip")
            check(
                chip.evaluate("e => getComputedStyle(e).borderStyle") == "dashed",
                "inferred chips have a dashed border",
            )
            page.screenshot(path=str(shots / "07-grid-after.png"))

            print("Open runs the one lookup")
            with context.expect_page() as popup_info:
                page.locator("#grid .card").first.locator(".open").click()
            popup = popup_info.value
            popup.wait_for_url(re.compile(r"^https://"), timeout=10000)
            popup.close()
            page.wait_for_load_state("networkidle")
            check("link lookups 1 of 12" in lookups_text(page), "Open ran one lookup")
            check(
                page.locator("#grid .card").first.locator(".resolved").inner_text() != "",
                "the opened card shows its retailer line",
            )

            print("delete an inferred chip; Saved; reload")
            before = page.locator("#pref-chips .chip.pref").count()
            chip.locator(".x").click()
            page.locator("#pref-chips .chip.pref").nth(before - 1).wait_for(state="detached")
            print("   chips now:", page.inner_text("#pref-chips").replace("\n", " | "))
            check("brushed brass" not in page.inner_text("#pref-chips"), "the chip is deleted")
            page.click("#saved-toggle")
            check(
                saved_counts(page) == ("Loved (2)", "Liked (2)"), f"Saved tabs {saved_counts(page)}"
            )
            page.reload()
            wait_for_results(page)
            check(card_ids(page) == new_ids, "the Shop Again batch is restored")
            check(page.locator("#saved").is_visible(), "Saved is still open")
            check(
                "brushed brass" not in page.inner_text("#pref-chips"),
                "the deleted chip is still deleted after a reload",
            )
            check(
                page.locator("#pref-chips .chip.pref.inferred").count() >= 1,
                "the other inferred chips are restored",
            )
            check("link lookups 1 of 12" in lookups_text(page), "the lookup count is restored")
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(shots / "09-mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 900})

            print("a guess chip stays when nothing contradicts it")
            page.click("#start-over")
            page.click("#start-over-yes")
            search(page, COFFEE)
            page.locator("#grid .card").nth(2).locator(".rate.like").click()
            page.click("#shop-again")
            wait_for_results(page)
            page.locator("#prefs").wait_for(state="visible")
            guess = page.locator("#pref-chips .chip.pref.guess")
            check(guess.count() == 1 and "industrial" in guess.first.inner_text(), "a guess chip")
            check(
                (guess.first.locator(".tag").text_content() or "") == "guess", "it is tagged guess"
            )
            check(
                guess.first.evaluate("e => getComputedStyle(e).borderStyle") == "dotted",
                "a guess chip has a dotted border (inferred is dashed)",
            )
            check(
                page.locator("#pref-chips .chip.pref.like").count() == 0
                or "industrial" not in page.inner_text("#pref-chips .chip.pref.like"),
                "a guess is not shown as a like",
            )
            page.screenshot(path=str(shots / "08-guess-chip.png"))
            guess.first.locator(".x").click()
            page.locator("#pref-chips .chip.pref.guess").first.wait_for(state="detached")
            check(
                page.locator("#pref-chips .chip.pref.guess").count() == 0,
                "the guess chip is deleted",
            )
            page.reload()
            wait_for_results(page)
            check(page.locator("#pref-chips .chip.pref.guess").count() == 0, "and stays deleted")

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
            run_checks(f"http://127.0.0.1:{port}/", OUT)
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
