"""Product thumbnails for the image-based style check (ADR 0010), fetched under strict rules.

This code fetches a URL that came from a third-party response, and may later run on a server, so it
treats the URL as untrusted:

  * https only, including every redirect (redirects are followed by hand, at most 3);
  * the host must resolve only to public addresses: private, loopback, link-local, reserved,
    multicast and unspecified addresses are refused, and so are IP-literal hosts in those ranges
    (a DNS answer could still change between the check and the connection; a server deployment
    should also restrict egress at the network level);
  * a hard 3 second deadline for the whole fetch, and at most 1 MB read;
  * the content type must be an image (SVG is refused), and the bytes must decode with Pillow;
  * the image is re-encoded small (`prepare_image`, which also strips metadata);
  * the URL's query string is never logged or put in a trace: only the host and a reason.

Any failure returns `ThumbResult(ok=False, reason=...)`; the caller falls back to the text-only
style check for that item and counts the failure in the trace. Mock mode never touches the network:
`MockThumbnailer` serves a small generated image for any URL.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx2 as httpx

from app.config import Config
from app.images import PreparedImage, prepare_image, synthetic_room_image
from app.tracing import span

TIMEOUT_S = 3.0
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
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


def blocked_address(ip: str) -> bool:
    """True for any address that is not a plain public unicast address."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return True  # not an address at all: refuse
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
        self, client: httpx.Client | None = None, resolver: Resolver | None = None
    ) -> None:
        self._client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._resolver = resolver or system_resolver

    def _check_host(self, url: str) -> str | None:
        """None when the URL is acceptable, else a short reason."""
        parts = urlsplit(url)
        if parts.scheme != "https":
            return "not https"
        host = parts.hostname
        if not host:
            return "no host"
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        try:
            addresses = [str(literal)] if literal is not None else self._resolver(host)
        except OSError:
            return "host did not resolve"
        if not addresses:
            return "host did not resolve"
        if any(blocked_address(a) for a in addresses):
            return "address not public"
        return None

    def fetch(self, url: str) -> ThumbResult:
        host = host_of(url)
        deadline = time.monotonic() + TIMEOUT_S
        current = url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                refused = self._check_host(current)
                if refused:
                    return ThumbResult(False, refused, host_of(current))
                with self._client.stream(
                    "GET", current, headers={"Accept": "image/*"}, timeout=TIMEOUT_S
                ) as response:
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
                    if declared.isdigit() and int(declared) > MAX_BYTES:
                        return ThumbResult(False, "too large", host)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_BYTES:
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
        image = prepare_image(data, max_edge=THUMB_MAX_EDGE)
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
