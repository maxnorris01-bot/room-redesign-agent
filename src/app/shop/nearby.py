"""The same product from different vendors (ADR 0012, stage 2n amendment).

The same product from several sellers is several search results with different ids, different
thumbnail URLs and slightly different titles, so the id, exact-thumbnail and exact-title checks of
`app.shop.dedupe` miss it. Two signals find it:

  * the image: a 64-bit difference hash (dHash) of the thumbnail; two images with a Hamming distance
    of at most `NEAR_FP_DISTANCE` are one image, and the prices must be within `NEAR_FP_PRICE`;
  * the title: lowercase, no punctuation, no seller names, no size or quantity words ("84 inch",
    "set of 2", "2 panels"), no filler; the word sets' Jaccard similarity is at least
    `NEAR_TITLE_JACCARD` and the prices are within `NEAR_TITLE_PRICE`.

Either signal makes two items one product. Items are compared inside the candidate pool and against
everything already shown or rated in the session (and what was hidden as a duplicate earlier), across
topics. `collapse` keeps one representative per group of the pool and remembers the others (for the
"Also at" line and the seen memory); against the seen memory a duplicate is simply dropped.

Fingerprints come from thumbnails fetched through the guarded fetcher in `app.thumbnails`: https only,
public addresses only, redirects checked by hand, at most 500 KB read, 3 seconds, a pixel limit
checked on the header before decoding, and any failure means "no hash", never a failed run. The live
step is bounded: at most `NEAR_FP_WORKERS` fetches at once, `NEAR_FP_BUDGET_S` seconds for all of
them, and only for the best ranked `NEAR_FP_TOP` candidates; what is not fingerprinted in time is
compared by title only, and the trace says so.
"""

from __future__ import annotations

import base64
import io
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from PIL import Image

from app.config import Config
from app.images import PreparedImage
from app.shop.retailers import AFFINITY_TIERS, RETAILERS, canonical_name, seller_key, tier_of
from app.shop.store import Session, in_topic, meta_kind_of
from app.shop.thresholds import (
    NEAR_FP_BUDGET_S,
    NEAR_FP_DISTANCE,
    NEAR_FP_PRICE,
    NEAR_FP_WORKERS,
    NEAR_TITLE_JACCARD,
    NEAR_TITLE_PRICE,
)
from app.thumbnails import HttpThumbnailer, Thumbnailer, fetch_traced

if TYPE_CHECKING:
    from app.shop.phototags import ThumbCache

FP_MAX_BYTES = 500_000
FP_PIXEL_LIMIT = 24_000_000
IMAGE_SIGNAL = "image_fingerprint"
TITLE_SIGNAL = "title_similarity"


# ---- the image signal -----------------------------------------------------------------------------------


def dhash_from_bytes(data: bytes) -> int | None:
    """The 64-bit difference hash of an image, or None when it cannot be hashed safely.

    A 9 by 8 grayscale copy; each bit says whether a pixel is brighter than its right neighbour.
    Refuses an image over `FP_PIXEL_LIMIT` pixels (read from the header, before any decoding), decodes
    a JPEG at a reduced size, and returns None for anything Pillow refuses."""
    if len(data) > FP_MAX_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if opened.width * opened.height > FP_PIXEL_LIMIT:
                return None
            opened.draft("L", (64, 64))
            gray = opened.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    except Exception:  # Pillow raises many types for hostile or broken data: all mean "no hash"
        return None
    pixels = list(gray.tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0)
    return bits


def dhash_of_prepared(image: PreparedImage) -> int | None:
    try:
        return dhash_from_bytes(base64.standard_b64decode(image.data_b64))
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class Fingerprinter(Protocol):
    """Gives the fingerprint of a thumbnail URL (fetching it) or of an image already fetched."""

    def fetch(self, url: str) -> int | None: ...

    def from_prepared(self, url: str, image: PreparedImage) -> int | None: ...


class HttpFingerprinter:
    """Real fingerprints: the guarded fetcher with a 500 KB cap, then `dhash_of_prepared`."""

    def __init__(
        self,
        config: Config,
        thumbnailer: Thumbnailer | None = None,
        cache: ThumbCache | None = None,
    ) -> None:
        self.config = config
        self._thumbnailer = thumbnailer or HttpThumbnailer(max_bytes=FP_MAX_BYTES)
        self._cache = cache  # stage 2p: the one place thumbnails are fetched (photo tags reuse it)

    def fetch(self, url: str) -> int | None:
        if not url.startswith("https://"):
            return None
        if self._cache is not None:  # fetched once, here or by the photo tagging step
            image = self._cache.image(url)
            return dhash_of_prepared(image) if image is not None else None
        try:
            got = fetch_traced(self._thumbnailer, url, self.config)
        except Exception:
            return None
        return dhash_of_prepared(got.image) if got.ok and got.image is not None else None

    def from_prepared(self, url: str, image: PreparedImage) -> int | None:
        return dhash_of_prepared(image)


_MOCK_ID = re.compile(r"/thumb/(\d+)")


class MockFingerprinter:
    """Offline: no bytes exist for a mock URL, so the hash comes from the image id in the path
    (`.../thumb/<id>...`): the same id is the same image, any other id an unrelated one."""

    def fetch(self, url: str) -> int | None:
        found = _MOCK_ID.search(url)
        if found is None:
            return None
        import hashlib

        return int.from_bytes(hashlib.sha256(found.group(1).encode()).digest()[:8], "big")

    def from_prepared(self, url: str, image: PreparedImage) -> int | None:
        return self.fetch(url)


def build_fingerprinter(config: Config, cache: ThumbCache | None = None) -> Fingerprinter | None:
    if not config.shop_near_duplicates or not config.thumbnails:
        return None
    if config.llm_mode == "mock":
        return MockFingerprinter()
    return HttpFingerprinter(config, cache=cache)


# ---- the title signal ---------------------------------------------------------------------------------


_SIZE_PATTERNS = (
    re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:inch(?:es)?|in|cm|mm|ft|feet|foot|yd|yds|yards?|oz|lbs?|\"|'')(?=\W|$)"
    ),
    re.compile(r"\b\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?\b"),
    re.compile(r"\b(?:set|pack|lot|pair)\s+of\s+\d+\b"),
    re.compile(r"\b\d+\s*(?:panels?|pcs?|pieces?|pack|count|ct|pr|pairs?)\b"),
)
_STOP = frozenset(
    "a an and the with for of in by to at on or from set free shipping new sale best official store "
    "inc llc pair piece pieces pc pcs panel panels inch inches pack".split()
)
_RETAILER_PHRASES: list[list[str]] | None = None


def _retailer_phrases() -> list[list[str]]:
    global _RETAILER_PHRASES
    if _RETAILER_PHRASES is None:
        names: set[str] = set()
        for r in RETAILERS:
            for n in (r.name, r.search_name, *r.aliases):
                words = re.sub(r"[^a-z0-9 ]+", " ", n.lower().replace("&", " and ")).split()
                if words:
                    names.add(" ".join(words))
        _RETAILER_PHRASES = [p.split() for p in sorted(names, key=len, reverse=True)]
    return _RETAILER_PHRASES


def _remove_phrase(words: list[str], phrase: list[str]) -> list[str]:
    n = len(phrase)
    if n == 0:
        return words
    out: list[str] = []
    i = 0
    while i < len(words):
        if words[i : i + n] == phrase:
            i += n
        else:
            out.append(words[i])
            i += 1
    return out


def normalize_title(title: str, seller: str = "") -> frozenset[str]:
    """The words that identify a product in a title: lowercase, no punctuation, no seller or retailer
    names, no size or quantity words, no filler. The empty set means nothing is left to compare."""
    text = title.lower().replace("″", '"').replace("”", '"').replace("&", " and ")
    for pattern in _SIZE_PATTERNS:
        text = pattern.sub(" ", text)
    words = re.sub(r"[^a-z0-9 ]+", " ", text).split()
    own = re.sub(r"[^a-z0-9 ]+", " ", canonical_name(seller).lower().replace("&", " and ")).split()
    words = _remove_phrase(words, own)
    for phrase in _retailer_phrases():
        words = _remove_phrase(words, phrase)
    return frozenset(w for w in words if len(w) >= 2 and w not in _STOP)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def prices_within(a: float | None, b: float | None, share: float) -> bool:
    """Both prices exist and differ by at most `share` of the larger (the boundary is inclusive)."""
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    return abs(a - b) <= share * max(a, b) + 1e-9


# ---- items, matching, collapsing ----------------------------------------------------------------------------


@dataclass
class NearItem:
    """What the near-duplicate signals look at for one listing."""

    key: str  # a product id (candidates) or a result id (shown, rated or hidden items)
    title: str
    seller: str
    price: float | None
    rating: float | None = None
    thumbnail: str = ""
    fingerprint: int | None = None
    tokens: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = normalize_title(self.title, self.seller)


def match_reason(a: NearItem, b: NearItem) -> str | None:
    """Why two listings are the same product, or None. The image signal is checked first."""
    if (
        a.fingerprint is not None
        and b.fingerprint is not None
        and hamming(a.fingerprint, b.fingerprint) <= NEAR_FP_DISTANCE
        and prices_within(a.price, b.price, NEAR_FP_PRICE)
    ):
        return IMAGE_SIGNAL
    if jaccard(a.tokens, b.tokens) >= NEAR_TITLE_JACCARD and prices_within(
        a.price, b.price, NEAR_TITLE_PRICE
    ):
        return TITLE_SIGNAL
    return None


@dataclass
class FingerprintReport:
    wanted: int = 0
    fetched: int = 0
    failed: int = 0
    skipped: int = 0  # not fingerprinted because the time budget ran out
    seconds: float = 0.0
    budget_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "wanted": self.wanted, "fetched": self.fetched, "failed": self.failed,
            "skipped": self.skipped, "seconds": round(self.seconds, 2), "budget_s": self.budget_s,
            "title_only_for_the_rest": self.skipped > 0,
        }  # fmt: skip


def fingerprint_many(
    items: Sequence[NearItem],
    fingerprinter: Fingerprinter,
    cache: dict[str, int | None],
    workers: int = NEAR_FP_WORKERS,
    budget_s: float = NEAR_FP_BUDGET_S,
) -> FingerprintReport:
    """Fingerprint the thumbnails of `items` that are not in `cache`, at most `workers` at once and
    for at most `budget_s` seconds in all; a fetch still running at the end is abandoned (its item is
    compared by title only). Failures are None in the cache (never asked again). Never raises."""
    todo = [i for i in items if i.key not in cache and i.thumbnail.startswith("https://")]
    report = FingerprintReport(wanted=len(todo), budget_s=budget_s)
    for i in items:  # what is already known fills in without a fetch
        if i.key in cache:
            i.fingerprint = cache[i.key]
    if not todo or budget_s <= 0:
        report.skipped = len(todo) if budget_s <= 0 else 0
        return report
    started = time.monotonic()
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo))))

    def one(item: NearItem) -> tuple[NearItem, int | None]:
        try:
            return item, fingerprinter.fetch(item.thumbnail)
        except Exception:
            return item, None

    futures = [pool.submit(one, i) for i in todo]
    done, pending = wait(futures, timeout=budget_s)
    pool.shutdown(wait=False, cancel_futures=True)
    for fut in done:
        item, value = fut.result()
        with lock:
            cache[item.key] = value
        item.fingerprint = value
        if value is None:
            report.failed += 1
        else:
            report.fetched += 1
    report.skipped = len(pending)
    report.seconds = time.monotonic() - started
    return report


@dataclass
class Collapse:
    """The outcome of collapsing a candidate list."""

    kept: list[int] = field(default_factory=list)  # indexes of `ordered` that stay
    groups: dict[int, list[int]] = field(default_factory=dict)  # kept index -> hidden members
    seen_hits: list[tuple[int, str, str]] = field(default_factory=list)  # (index, seen key, reason)
    by_reason: dict[str, int] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)


def representative(members: Sequence[NearItem], preferred: Callable[[NearItem], bool]) -> int:
    """The index (into `members`) of the one to show: a seller that earned a seller or tier chip or
    is design premium first, then one with a rating, then the lowest price, then the best ranked."""

    def key(i: int) -> tuple[int, int, float, int]:
        m = members[i]
        return (
            0 if preferred(m) else 1,
            0 if m.rating is not None else 1,
            m.price if m.price is not None else float("inf"),
            i,
        )

    return min(range(len(members)), key=key)


def collapse(
    items: Sequence[NearItem],
    seen: Sequence[NearItem],
    preferred: Callable[[NearItem], bool],
    max_examples: int = 5,
) -> Collapse:
    """Drop what repeats something already shown or rated, then collapse the rest into groups of one
    product and keep one representative per group (see `representative`; it takes the place of the
    group's best ranked member). `items` are in rank order."""
    out = Collapse()
    live: list[int] = []
    for n, item in enumerate(items):
        hit = next(((s, r) for s in seen if (r := match_reason(item, s)) is not None), None)
        if hit is not None:
            out.seen_hits.append((n, hit[0].key, hit[1]))
            out.by_reason[hit[1]] = out.by_reason.get(hit[1], 0) + 1
            continue
        live.append(n)
    parent = {n: n for n in live}
    reason_of: dict[int, str] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a_pos, a in enumerate(live):
        for b in live[a_pos + 1 :]:
            reason = match_reason(items[a], items[b])
            if reason is not None:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
                reason_of[b] = reason_of.get(b, reason)
    clusters: dict[int, list[int]] = {}
    for n in live:
        clusters.setdefault(find(n), []).append(n)
    chosen: dict[int, int] = {}  # cluster root -> kept index
    for root, members in clusters.items():
        pick = members[representative([items[m] for m in members], preferred)]
        chosen[root] = pick
        if len(members) > 1:
            out.groups[pick] = [m for m in members if m != pick]
            for m in members:
                if m != pick:
                    why = reason_of.get(m) or reason_of.get(pick) or TITLE_SIGNAL
                    out.by_reason[why] = out.by_reason.get(why, 0) + 1
            if len(out.examples) < max_examples:
                out.examples.append(
                    {
                        "kept": f"{items[pick].title[:60]} ({canonical_name(items[pick].seller)})",
                        "also": [
                            f"{items[m].title[:60]} ({canonical_name(items[m].seller)})"
                            for m in members
                            if m != pick
                        ][:3],
                    }
                )
    # the representative stands where the group's best ranked member stood
    out.kept = sorted(chosen.values(), key=lambda k: min(clusters[find(k)]))
    return out


def preferred_seller(chip_sellers: set[str], chip_tier: str = "") -> Callable[[NearItem], bool]:
    """A seller that earned a seller or tier chip, or is design premium."""

    def check(item: NearItem) -> bool:
        key = seller_key(item.seller)
        tier = tier_of(item.seller)
        return (
            key in chip_sellers
            or (bool(chip_tier) and tier == chip_tier)
            or tier == AFFINITY_TIERS[0]
        )

    return check


def seen_items(session: Session) -> list[NearItem]:
    """The seen memory as near-duplicate items: every result ever shown or rated in the session (any
    topic), with its fingerprint when one is known, and everything hidden as a duplicate before."""
    out = [
        NearItem(
            key=r.id, title=r.title, seller=r.seller, price=r.price, rating=r.rating,
            thumbnail=r.thumbnail_url, fingerprint=session.fingerprints.get(r.id),
        )
        for r in session.results.values()
    ]  # fmt: skip
    out += [
        NearItem(
            key=key, title=row.get("title", ""), seller=row.get("seller", ""),
            price=row.get("price"), thumbnail=row.get("thumbnail", ""),
            fingerprint=row.get("fingerprint"),
        )
        for key, row in session.hidden.items()
    ]  # fmt: skip
    return out


def also_at_text(members: Sequence[tuple[str, float | None]], shown: int = 3) -> str:
    """ "Also at: eBay $18.99, Lowe's $21.00 and 2 more" (text only; no links)."""
    parts = [
        f"{name} ${price:,.2f}" if price is not None else name for name, price in members[:shown]
    ]
    text = "Also at: " + ", ".join(parts)
    if len(members) > shown:
        text += f" and {len(members) - shown} more"
    return text


MAX_META_TRACE = 20


def meta_trace(session: Session) -> list[dict[str, Any]]:
    """The items whose comment reported a listing problem instead of taste, for the trace: id,
    title, the class (and its label), whether it is a duplicate the shopper flagged, and whether the
    automatic duplicate check would have caught it (some other seen listing matches it by image or
    title). Newest rating first, at most `MAX_META_TRACE`."""
    from app.shop.prefs import META_LABELS  # late: prefs does not import this module

    seen = seen_items(session)
    rows: list[dict[str, Any]] = []
    flagged = [
        (rid, rec, kind)
        for rid, rec in session.ratings.items()
        if rid in session.results
        and in_topic(rec, session)
        and (kind := meta_kind_of(session, rid, rec)) is not None
    ]
    flagged.sort(key=lambda row: row[1].updated_at or "", reverse=True)
    for rid, rec, kind in flagged[:MAX_META_TRACE]:
        shown = session.results[rid]
        me = next((it for it in seen if it.key == rid), None)
        caught = None
        if me is not None:
            caught = next(
                (
                    (other, reason)
                    for other in seen
                    if other.key != rid and (reason := match_reason(me, other)) is not None
                ),
                None,
            )
        rows.append(
            {
                "id": rid,
                "title": shown.title[:120],
                "class": kind,
                "label": META_LABELS.get(kind, kind),
                "flagged_duplicate": kind == "duplicate",
                "dedupe_would_catch": caught is not None,
                "caught_by": caught[1] if caught else "",
                "comment": rec.comment[:120],
            }
        )
    return rows
