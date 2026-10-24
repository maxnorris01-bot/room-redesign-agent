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


def run_checks(base: str, shots: Path) -> None:
    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            requested = install_routes(context, 0)
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
            check(page.locator("#shop-again").is_disabled(), "Shop Again is disabled")
            check(
                page.locator(".again").get_attribute("title") == "Coming next",
                "Shop Again tooltip says Coming next",
            )
            page.screenshot(path=str(shots / "01-empty.png"))

            for slug, text in EXAMPLES:
                print(f"search: {text}")
                search(page, text)
                n = page.locator(".card").count()
                check(0 < n <= 24, f"{n} result cards")
                print("   chips:", " | ".join(chips(page)))
                print("   footer:", page.inner_text("#stats"))
                check("Serper call" in page.inner_text("#stats"), "footer shows Serper calls")
                check("est. $" in page.inner_text("#stats"), "footer shows the estimated cost")
                page.screenshot(path=str(shots / f"02-results-{slug}.png"))
            full = shots / "03-results-curtains-full.png"
            page.screenshot(path=str(full), full_page=True)

            # price chip check on the first example
            search(page, EXAMPLES[0][1])
            check(any("up to $1,000" in c for c in chips(page)), "price chip reads up to $1,000")
            check(any(c.endswith("modern") for c in chips(page)), "style chip shows modern")
            prices = [
                float(t.replace("$", "").replace(",", ""))
                for t in page.locator(".card .price:not(.na)").all_inner_texts()
            ]
            check(
                bool(prices) and max(prices) <= 1000,
                f"all prices at or under $1,000 ({len(prices)})",
            )

            print("rate three results, comment, open one")
            cards = page.locator(".card")
            ids = [cards.nth(i).get_attribute("data-id") for i in range(3)]
            love, like, dislike = (cards.nth(i) for i in range(3))
            love.locator(".rate.love").click()
            like.locator(".rate.like").click()
            dislike.locator(".rate.dislike").click()
            for card, kind in ((love, "love"), (like, "like"), (dislike, "dislike")):
                check(
                    card.locator(f".rate.{kind}").get_attribute("aria-pressed") == "true",
                    f"{kind} highlighted",
                )
            check("disliked" in (dislike.get_attribute("class") or ""), "disliked card is dimmed")
            like.locator(".rate.like").click()
            check(
                like.locator(".rate.like").get_attribute("aria-pressed") == "false",
                "clicking again clears the rating",
            )
            like.locator(".rate.like").click()
            note = love.locator(".note")
            note.fill("Love the arms, check the depth")
            note.press("Enter")
            dislike.locator(".note").fill("Too dark for the room")
            page.locator("h1").click()  # blur saves the second note
            love.locator(".resolved").filter(has_text=re.compile(".+")).wait_for(timeout=10000)
            check(
                "Sold by" in love.locator(".resolved").inner_text()
                or "Google" in love.locator(".resolved").inner_text(),
                "loving a result resolves its link",
            )
            with context.expect_page() as popup_info:
                like.locator(".open").click()
            popup = popup_info.value
            popup.wait_for_url(re.compile(r"^https://"), timeout=10000)
            popup.wait_for_load_state()
            check(
                urlsplit(popup.url).scheme == "https" and "Mock retailer" in popup.title(),
                f"Open goes to the retailer link in a new tab ({urlsplit(popup.url).hostname})",
            )
            popup.close()
            page.wait_for_load_state("networkidle")
            lookups = page.inner_text("#stats")
            print("   footer:", lookups)
            check("link lookups 2 of 12" in lookups, "two lookups used (love, open)")
            page.screenshot(path=str(shots / "04-rated.png"))

            print("reload")
            page.reload()
            wait_for_results(page)
            cards = page.locator(".card")
            check(
                [cards.nth(i).get_attribute("data-id") for i in range(3)] == ids,
                "same results restored in the same order",
            )
            check(
                cards.nth(0).locator(".rate.love").get_attribute("aria-pressed") == "true",
                "love restored",
            )
            check(
                cards.nth(1).locator(".rate.like").get_attribute("aria-pressed") == "true",
                "like restored",
            )
            check(
                cards.nth(2).locator(".rate.dislike").get_attribute("aria-pressed") == "true",
                "dislike restored",
            )
            check(
                cards.nth(0).locator(".note").input_value() == "Love the arms, check the depth",
                "comment restored",
            )
            check(
                cards.nth(2).locator(".note").input_value() == "Too dark for the room",
                "comment saved on blur restored",
            )
            check(
                cards.nth(1).locator(".resolved").inner_text() != "",
                "resolved retailer info restored",
            )
            check(page.input_value("#request") == EXAMPLES[0][1], "search box restored")
            check(any("up to $1,000" in c for c in chips(page)), "chips restored")
            check("link lookups 2 of 12" in page.inner_text("#stats"), "lookup count restored")
            page.screenshot(path=str(shots / "05-restored.png"))

            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(shots / "06-restored-mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 900})

            hosts = sorted(set(requested))
            print("non-local hosts the page asked for (all answered by this script):", hosts)
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
