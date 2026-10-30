"""Product thumbnails for the image-based style check (ADR 0010), fetched under strict rules.

This code fetches a URL that came from a third-party response, and may later run on a server, so it
treats the URL as untrusted:

  * https only, including every redirect (redirects are followed by hand, at most 3);
  * the host is resolved ONCE per request and must resolve only to public addresses: private,
    loopback, link-local, unique-local (including Fly's fdaa::/16), carrier-grade NAT, reserved,
    multicast and unspecified addresses are refused, IPv4-mapped IPv6 addresses are judged by the
    IPv4 address inside, and so are IP-literal hosts in those ranges. The connection then goes to
    the address that was checked (pinned: the URL carries the IP, the Host header and the TLS server
    name stay the original host), so a DNS answer that changes between the check and the connection
    cannot send the request somewhere else (stage D1; this closed the rebinding gap);
  * a redirect may only stay on the same host (it is checked and pinned again);
  * a hard 3 second deadline for the whole fetch, and at most 1 MB read;
  * the content type must be an image (SVG is refused), and the bytes must decode with Pillow;
  * the image is re-encoded small (`prepare_image`, which also strips metadata);
  * the URL's query string is never logged or put in a trace: only the host and a reason.

Any failure returns `ThumbResult(ok=False, reason=...)`; the caller falls back to the text-only
style check for that item and counts the failure in the trace. Mock mode never touches the network:
`MockThumbnailer` serves a small generated image for any URL.
"""

from __future__ import annotations

import io
import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx2 as httpx
from PIL import Image

from app.config import Config
from app.images import PreparedImage, prepare_image, synthetic_room_image
from app.tracing import span

TIMEOUT_S = 3.0
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
MAX_PIXELS = 24_000_000  # a thumbnail with more pixels than this is refused before decoding
THUMB_MAX_EDGE = 256


@dataclass(frozen=True)
class ThumbResult:
    ok: bool
    reason: str  # "ok" or a short failure reason; never contains the URL
    host: str = ""
    image: PreparedImage | None = None
    n_bytes: int = 0


class Thumbnailer(Protocol):
    def fetch(self, url: str) -> ThumbResult: ...


Resolver = Callable[[str], list[str]]


def host_of(url: str) -> str:
    """The host only (never the path or query), for traces."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10", "fdaa::/16",
    )
)  # fmt: skip


def blocked_address(ip: str) -> bool:
    """True for any address that is not a plain public unicast address."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return True  # not an address at all: refuse
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return blocked_address(str(addr.ipv4_mapped))  # ::ffff:10.0.0.1 is 10.0.0.1
    if any(addr in net for net in BLOCKED_NETWORKS if net.version == addr.version):
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or not addr.is_global
    )


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class HttpThumbnailer:
    """Fetches thumbnails over https. `client` and `resolver` can be injected (tests)."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        resolver: Resolver | None = None,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._resolver = resolver or system_resolver
        self._max_bytes = max_bytes  # a fingerprint fetcher reads at most 500 KB

    def _check_host(self, url: str) -> tuple[str | None, str]:
        """(a short reason or None, the address to connect to). The host is resolved here, once."""
        parts = urlsplit(url)
        if parts.scheme != "https":
            return "not https", ""
        host = parts.hostname
        if not host:
            return "no host", ""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        try:
            addresses = [str(literal)] if literal is not None else self._resolver(host)
        except OSError:
            return "host did not resolve", ""
        if not addresses:
            return "host did not resolve", ""
        if any(blocked_address(a) for a in addresses):
            return "address not public", ""
        return None, addresses[0]

    @staticmethod
    def _pinned(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
        """(the URL with the checked address in place of the host, the headers that keep the
        original host, the extensions that keep the TLS server name)."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        shown = f"[{address}]" if ":" in address else address
        netloc = shown + (f":{parts.port}" if parts.port else "")
        pinned = parts._replace(netloc=netloc).geturl()
        authority = host + (f":{parts.port}" if parts.port else "")
        return pinned, {"Host": authority}, {"sni_hostname": host}

    def fetch(self, url: str) -> ThumbResult:
        host = host_of(url)
        deadline = time.monotonic() + TIMEOUT_S
        current = url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                if urlsplit(current).scheme != "https":
                    return ThumbResult(False, "not https", host_of(current))
                if host_of(current) != host:  # never even looked up
                    return ThumbResult(False, "redirect to another host", host_of(current))
                refused, address = self._check_host(current)
                if refused:
                    return ThumbResult(False, refused, host_of(current))
                target, extra_headers, extensions = self._pinned(current, address)
                with self._client.stream(
                    "GET", target, headers={"Accept": "image/*", **extra_headers},
                    timeout=TIMEOUT_S, extensions=extensions,
                ) as response:  # fmt: skip
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location")
                        if not location:
                            return ThumbResult(False, "redirect without location", host)
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        return ThumbResult(False, f"HTTP {response.status_code}", host)
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not kind.startswith("image/") or "svg" in kind:
                        return ThumbResult(False, "not an image", host)
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > self._max_bytes:
                        return ThumbResult(False, "too large", host)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            return ThumbResult(False, "too large", host)
                        if time.monotonic() > deadline:
                            return ThumbResult(False, "timed out", host)
                        chunks.append(chunk)
                    return _decode(b"".join(chunks), host)
            return ThumbResult(False, "too many redirects", host)
        except httpx.TimeoutException:
            return ThumbResult(False, "timed out", host)
        except httpx.HTTPError as exc:
            return ThumbResult(False, f"request failed ({type(exc).__name__})", host)


def _decode(data: bytes, host: str) -> ThumbResult:
    try:
        with Image.open(
            io.BytesIO(data)
        ) as header:  # the header only: a decompression bomb is refused
            if header.width * header.height > MAX_PIXELS:
                return ThumbResult(False, "too many pixels", host, n_bytes=len(data))
        image = prepare_image(data, max_edge=THUMB_MAX_EDGE, draft=True)
    except Exception:  # Pillow raises several types for bad data; all mean "not usable"
        return ThumbResult(False, "not a decodable image", host, n_bytes=len(data))
    return ThumbResult(True, "ok", host, image, len(data))


class MockThumbnailer:
    """Offline thumbnails: a small generated image for any URL. Never touches the network."""

    def __init__(self) -> None:
        self._image = prepare_image(synthetic_room_image(96, 64), max_edge=THUMB_MAX_EDGE)
        self.urls: list[str] = []

    def fetch(self, url: str) -> ThumbResult:
        self.urls.append(url)
        return ThumbResult(True, "ok", host_of(url), self._image, 1)


def build_thumbnailer(config: Config) -> Thumbnailer | None:
    """None when thumbnails are off; the offline generator in mock mode; HTTP in live mode."""
    if not config.thumbnails:
        return None
    return MockThumbnailer() if config.llm_mode == "mock" else HttpThumbnailer()


def fetch_traced(thumbnailer: Thumbnailer, url: str, config: Config) -> ThumbResult:
    """Fetch and record one trace span (host and outcome only; never the URL or its query)."""
    with span("thumbnail.fetch", config=config, host=host_of(url)) as rec:
        result = thumbnailer.fetch(url)
        rec.update(ok=result.ok, reason=result.reason, bytes=result.n_bytes)
    return result
