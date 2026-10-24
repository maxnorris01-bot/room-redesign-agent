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
            check(page.inner_text("#saved-toggle") == "Show saved (0)", "Saved toggle starts at 0")
            page.screenshot(path=str(shots / "01-empty.png"))

            print("a failed search shows a readable error and a retry")
            page.route(
                "**/api/search",
                lambda r: r.fulfill(
                    status=502,
                    content_type="application/json",
                    body='{"error": "serper: HTTP 402 payment required"}',
                ),
            )
            page.fill("#request", COFFEE)
            page.click("#search-btn")
            page.locator("#status.error").wait_for()
            check("serper: HTTP 402" in page.inner_text("#status"), "error message is shown")
            check(page.locator("#retry").is_visible(), "a Retry button is shown")
            page.screenshot(path=str(shots / "02-error-retry.png"))
            page.unroute("**/api/search")
            page.click("#retry")
            wait_for_results(page)
            check(page.locator("#retry").is_hidden(), "Retry goes away after it works")

            print("search with a visible loading state")
            page.fill("#request", COFFEE)
            before = page.locator("#grid").bounding_box()
            release = hold(page, "**/api/search")
            page.click("#search-btn")
            page.locator("#spinner").wait_for(state="visible")
            check(
                page.inner_text("#status") == "Searching, usually about 10 seconds",
                "loading text says Searching, usually about 10 seconds",
            )
            check(page.locator("#search-btn").is_disabled(), "Search is disabled while loading")
            check(page.locator("#shop-again").is_disabled(), "Shop Again is disabled while loading")
            during = page.locator("#grid").bounding_box()
            check(
                before is not None
                and during is not None
                and abs(before["y"] - during["y"]) < 1
                and during["height"] >= before["height"] - 1,
                "the grid keeps its place and height while loading",
            )
            page.screenshot(path=str(shots / "03-loading.png"))
            release()
            wait_for_results(page)
            after = page.locator("#grid").bounding_box()
            check(
                after is not None and abs(after["y"] - (before or {"y": 0})["y"]) < 1,
                "results arrive in the same place",
            )
            check(page.locator("#spinner").is_hidden(), "spinner gone after results")
            check(page.locator("#search-btn").is_enabled(), "Search is enabled again")
            check(
                "Serper calls = 4 credits ($0.0040)" in page.inner_text("#stats"),
                "footer shows credits and cost",
            )
            print("   footer:", page.inner_text("#stats"))
            first_ids = card_ids(page)
            check(len(first_ids) == 24, f"{len(first_ids)} results")

            print("rate three results, one with a comment")
            cards = page.locator("#grid .card")
            love, like, dislike = (cards.nth(i) for i in range(3))
            love.locator(".rate.love").click()
            check(page.locator("#shop-again").is_enabled(), "Shop Again is enabled after a rating")
            check(page.locator("#again-wrap").get_attribute("title") is None, "tooltip gone")
            like.locator(".rate.like").click()
            dislike.locator(".rate.dislike").click()
            note = dislike.locator(".note")
            note.fill("too dark")
            note.press("Enter")
            love.locator(".resolved").filter(has_text=re.compile(".+")).wait_for(timeout=10000)
            page.wait_for_load_state("networkidle")
            check(
                page.inner_text("#saved-toggle") == "Show saved (2)",
                "Saved count is 2 (loved + liked)",
            )
            page.screenshot(path=str(shots / "04-rated.png"))

            print("Shop Again")
            grid_y = (page.locator("#grid").bounding_box() or {"y": -1})["y"]
            release = hold(page, "**/api/shop-again")
            page.click("#shop-again")
            page.locator("#spinner").wait_for(state="visible")
            check(
                page.inner_text("#status") == "Finding new results, usually about 10 seconds",
                "Shop Again loading text",
            )
            check(page.locator("#shop-again").is_disabled(), "Shop Again disabled while it runs")
            page.screenshot(path=str(shots / "05-shop-again-loading.png"))
            release()
            wait_for_results(page)
            page.locator("#prefs").wait_for(state="visible")
            check(
                abs((page.locator("#grid").bounding_box() or {"y": -9})["y"] - grid_y) < 1,
                "the grid does not move when Shop Again brings the preference chips",
            )
            new_ids = card_ids(page)
            check(len(new_ids) == 24, f"{len(new_ids)} new results")
            check(
                not set(new_ids) & set(first_ids), "no result from the first search is shown again"
            )
            print("   summary:", page.inner_text("#prefs-summary"))
            print("   footer:", page.inner_text("#stats"))
            pref_chips = page.locator("#pref-chips .chip.pref")
            check(pref_chips.count() >= 2, f"{pref_chips.count()} preference chips")
            check(page.locator("#pref-chips .chip.pref.like").count() >= 1, "a like chip with +")
            check(
                page.locator("#pref-chips .chip.pref.dislike").count() >= 1, "a dislike chip with -"
            )
            check(
                "too dark" in page.inner_text("#pref-chips"), "the 'too dark' comment became a chip"
            )
            check(
                "fewer too dark" in page.inner_text("#prefs-summary"),
                "summary mentions fewer too dark",
            )
            check(
                "Shop Again took" in page.inner_text("#stats")
                and "credits" in page.inner_text("#stats"),
                "footer is the Shop Again's",
            )
            page.screenshot(path=str(shots / "06-after-shop-again.png"))

            print("delete a chip")
            chips_before = pref_chips.count()
            gone_text = pref_chips.first.inner_text()
            pref_chips.first.locator(".x").click()
            page.locator("#pref-chips .chip.pref").nth(chips_before - 1).wait_for(state="detached")
            check(
                page.locator("#pref-chips .chip.pref").count() == chips_before - 1,
                "the chip is gone",
            )
            check(
                gone_text.strip("+−\n ×") not in page.inner_text("#pref-chips"),
                "its text is no longer shown",
            )

            print("Saved panel")
            check(page.locator("#saved").is_hidden(), "Saved is closed at first")
            page.click("#saved-toggle")
            check(page.locator("#saved").is_visible(), "Show saved opens the panel")
            check(
                page.inner_text("#saved-toggle") == "Hide saved (2)", "toggle reads Hide saved (2)"
            )
            check(saved_counts(page) == ("Loved (1)", "Liked (1)"), f"tabs {saved_counts(page)}")
            loved_ids = card_ids(page, "#saved-row")
            check(loved_ids == [first_ids[0]], "the loved item from the first search is in Saved")
            page.click("#tab-liked")
            check(card_ids(page, "#saved-row") == [first_ids[1]], "the liked item is under Liked")
            page.screenshot(path=str(shots / "07-saved-open.png"))
            page.click("#saved-toggle")
            check(page.locator("#saved").is_hidden(), "Hide saved closes the panel")
            page.click("#saved-toggle")

            print("re-rating from Saved")
            newcard = page.locator("#grid .card").first
            newcard.locator(".rate.like").click()
            check(page.inner_text("#tab-liked") == "Liked (2)", "a new like joins Saved")
            page.locator("#saved-row .card").nth(0).locator(".rate.like").click()
            check(
                page.inner_text("#tab-liked") == "Liked (1)",
                "clearing a rating in Saved removes it",
            )
            check(
                newcard.locator(".rate.like").get_attribute("aria-pressed") == "false",
                "the same card in the grid is cleared too",
            )
            page.locator("#saved-row .card").first.locator(".note").fill("good arms")
            page.locator("h1").click()
            page.click("#tab-loved")

            print("a second typed search keeps Saved")
            search(page, "boho curtains")
            check(
                saved_counts(page) == ("Loved (1)", "Liked (1)"),
                "Saved is unchanged by a new search",
            )
            check(page.locator("#saved").is_visible(), "the panel stays open")
            third_ids = card_ids(page)

            print("reload")
            page.reload()
            wait_for_results(page)
            check(card_ids(page) == third_ids, "the last search is restored")
            check(page.locator("#saved").is_visible(), "the Saved panel is still open (remembered)")
            check(saved_counts(page) == ("Loved (1)", "Liked (1)"), "Saved counts restored")
            check(card_ids(page, "#saved-row") == [first_ids[0]], "the loved card is restored")
            check(
                page.locator("#saved-row .card")
                .first.locator(".rate.love")
                .get_attribute("aria-pressed")
                == "true",
                "its love state is restored",
            )
            check(page.locator("#prefs").is_visible(), "preference chips restored")
            check(
                page.locator("#pref-chips .chip.pref").count() == chips_before - 1,
                "the deleted chip is still deleted",
            )
            page.click("#tab-liked")
            check(
                page.locator("#saved-row .card").first.locator(".note").input_value()
                == "good arms",
                "a note typed in Saved is restored",
            )
            check(page.locator("#shop-again").is_enabled(), "Shop Again is enabled after reload")
            page.screenshot(path=str(shots / "08-restored.png"))

            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(shots / "09-mobile.png"))
            page.evaluate("window.scrollTo(0, 0)")
            page.set_viewport_size({"width": 1280, "height": 900})

            print("start over")
            page.click("#start-over")
            check(page.locator("#start-over-confirm").is_visible(), "start over asks first")
            page.click("#start-over-cancel")
            check(page.locator("#grid .card").count() > 0, "cancel changes nothing")
            page.click("#start-over")
            page.click("#start-over-yes")
            check(page.locator("#grid .card").count() == 0, "start over empties the page")
            check(
                page.inner_text("#saved-toggle") == "Hide saved (0)",
                "Saved is empty in the new session",
            )
            check(page.locator("#prefs").is_hidden(), "preference chips cleared")
            page.reload()
            check(
                page.locator("#grid .card").count() == 0, "a reload stays in the new empty session"
            )
            check(console_errors == [], f"no console or page errors {console_errors[:3]}")
        finally:
            browser.close()


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
