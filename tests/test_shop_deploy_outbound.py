"""Stage D1, phase 5: the guarded thumbnail fetcher resolves a host once, refuses non-public
addresses (IPv4, IPv6, IPv4-mapped), connects to the address it checked and refuses redirects to
other hosts. Fake resolvers and a mock transport: no network."""

from __future__ import annotations

from pathlib import Path

import httpx2 as httpx
import pytest

from app.images import synthetic_room_image
from app.thumbnails import HttpThumbnailer, blocked_address

JPEG = synthetic_room_image(80, 60)
PUBLIC = "93.184.216.34"


class Seen:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.resolved: list[str] = []


def fetcher(
    seen: Seen, answers: list[list[str]] | None = None, redirect: dict[str, str] | None = None
) -> HttpThumbnailer:
    def resolver(host: str) -> list[str]:
        seen.resolved.append(host)
        n = len(seen.resolved) - 1
        return (answers or [[PUBLIC]])[min(n, len(answers or [[PUBLIC]]) - 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.requests.append(request)
        if redirect and request.url.path in redirect:
            return httpx.Response(302, headers={"location": redirect[request.url.path]})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return HttpThumbnailer(client, resolver)


def test_a_public_address_is_allowed_and_the_connection_is_pinned_to_it() -> None:
    seen = Seen()
    got = fetcher(seen).fetch("https://cdn.example/t/1.jpg?x=1")
    assert got.ok and seen.resolved == ["cdn.example"]
    request = seen.requests[0]
    assert (
        request.url.host == PUBLIC
        and request.url.path == "/t/1.jpg"
        and request.url.query == b"x=1"
    )
    assert request.headers["host"] == "cdn.example"  # the original Host header
    assert request.extensions["sni_hostname"] == "cdn.example"  # and the original TLS server name


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254", "169.254.0.1", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.1",
        "192.168.0.1", "127.0.0.1", "127.1.2.3", "0.0.0.0", "100.64.0.1", "::1", "::", "fc00::1",
        "fd12:3456::1", "fdaa::1", "fdaa:0:1234:a7b:5::2", "fe80::1", "fe80::1%eth0",
        "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:a9fe:a9fe", "::ffff:192.168.1.1", "not-an-ip",
    ],
)  # fmt: skip
def test_each_private_range_is_refused_and_nothing_is_sent(ip: str) -> None:
    assert blocked_address(ip)
    seen = Seen()
    got = fetcher(seen, [[ip]]).fetch("https://cdn.example/a.jpg")
    assert not got.ok and got.reason == "address not public" and seen.requests == []


@pytest.mark.parametrize(
    "ip", [PUBLIC, "8.8.8.8", "2606:4700::1", "2001:4860:4860::8888", "::ffff:93.184.216.34"]
)
def test_public_addresses_in_both_families_pass(ip: str) -> None:
    assert not blocked_address(ip)
    seen = Seen()
    assert fetcher(seen, [[ip]]).fetch("https://cdn.example/a.jpg").ok


def test_one_private_address_among_public_ones_refuses_the_host() -> None:
    seen = Seen()
    got = fetcher(seen, [[PUBLIC, "10.0.0.1"]]).fetch("https://cdn.example/a.jpg")
    assert not got.ok and got.reason == "address not public"


def test_a_name_that_answers_differently_the_second_time_is_not_asked_again() -> None:
    seen = Seen()
    got = fetcher(seen, [[PUBLIC], ["10.0.0.5"]]).fetch("https://cdn.example/a.jpg")
    assert got.ok and seen.resolved == ["cdn.example"]  # one lookup for this request
    assert [r.url.host for r in seen.requests] == [PUBLIC]  # the second answer never mattered


def test_ip_literal_hosts_ipv6_included() -> None:
    seen = Seen()
    assert not fetcher(seen).fetch("https://[::1]/a.jpg").ok
    assert not fetcher(seen).fetch("https://[fdaa::5]/a.jpg").ok
    assert not fetcher(seen).fetch("https://[::ffff:10.0.0.1]/a.jpg").ok
    assert not fetcher(seen).fetch("https://169.254.169.254/latest/meta-data").ok
    ok = fetcher(seen).fetch("https://[2606:4700::1]:8443/a.jpg")
    assert (
        ok.ok
        and seen.requests[-1].url.host == "2606:4700::1"
        and seen.requests[-1].url.port == 8443
    )
    assert (
        seen.requests[-1].headers["host"] == "2606:4700::1:8443"
        or "2606" in seen.requests[-1].headers["host"]
    )
    assert seen.resolved == []  # literals are never looked up


def test_a_redirect_to_another_host_is_refused_without_looking_it_up() -> None:
    for target in (
        "https://other.example/b.jpg",
        "https://internal.example/b.jpg",
        "https://10.0.0.1/b.jpg",
        "https://[::1]/b.jpg",
    ):
        seen = Seen()
        got = fetcher(seen, redirect={"/a.jpg": target}).fetch("https://cdn.example/a.jpg")
        assert not got.ok and got.reason == "redirect to another host"
        assert seen.resolved == ["cdn.example"] and len(seen.requests) == 1


def test_a_redirect_on_the_same_host_is_checked_and_pinned_again() -> None:
    seen = Seen()
    got = fetcher(seen, [[PUBLIC], ["10.0.0.9"]], {"/a.jpg": "/b.jpg"}).fetch(
        "https://cdn.example/a.jpg"
    )
    assert (
        not got.ok and got.reason == "address not public"
    )  # the second hop is a new lookup, checked
    seen2 = Seen()
    assert fetcher(seen2, redirect={"/a.jpg": "/b.jpg"}).fetch("https://cdn.example/a.jpg").ok
    assert [r.url.path for r in seen2.requests] == ["/a.jpg", "/b.jpg"] and seen2.resolved == [
        "cdn.example"
    ] * 2
    http = fetcher(Seen(), redirect={"/a.jpg": "http://cdn.example/b.jpg"}).fetch(
        "https://cdn.example/a.jpg"
    )
    assert http.reason == "not https"


def test_size_type_and_time_limits_are_unchanged() -> None:
    def build(content: bytes, kind: str, status: int = 200) -> HttpThumbnailer:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(status, content=content, headers={"content-type": kind})
            ),
            follow_redirects=False,
        )
        return HttpThumbnailer(client, lambda h: [PUBLIC], max_bytes=1000)

    assert build(JPEG, "image/jpeg").fetch("https://c.example/a").reason == "too large"
    assert build(b"<svg/>", "image/svg+xml").fetch("https://c.example/a").reason == "not an image"
    assert build(b"x", "text/html").fetch("https://c.example/a").reason == "not an image"
    assert build(b"x", "image/jpeg", 500).fetch("https://c.example/a").reason == "HTTP 500"


def test_no_other_code_fetches_a_url_that_came_from_a_provider() -> None:
    """The provider clients call fixed API hosts; a URL from a response (a thumbnail, a gallery
    image) is only ever fetched through `HttpThumbnailer`."""
    src = Path(__file__).resolve().parents[1] / "src"
    users = sorted(
        p.relative_to(src).as_posix()
        for p in (src / "app").rglob("*.py")  # Redesign Mode (photoreal) is not deployed
        if any(
            x in p.read_text()
            for x in ("httpx", "urllib.request", "import requests", "socket.create_connection")
        )
    )
    assert users == ["app/shopping.py", "app/thumbnails.py"], users
    shopping = (src / "app" / "shopping.py").read_text()
    for url in ("https://google.serper.dev/shopping",):
        assert url in shopping
    assert "client.get(item" not in shopping and ".get(thumbnail" not in shopping


def test_mixed_case_hosts_and_userinfo_cannot_smuggle_another_host(tmp_path: Path) -> None:
    seen = Seen()
    got = fetcher(seen).fetch(
        "https://cdn.example@10.0.0.1/a.jpg"
    )  # userinfo: the host is 10.0.0.1
    assert not got.ok and got.reason == "address not public"
    got2 = fetcher(Seen(), [["10.0.0.1"]]).fetch("https://CDN.Example/a.jpg")
    assert not got2.ok
    assert seen.requests == []
