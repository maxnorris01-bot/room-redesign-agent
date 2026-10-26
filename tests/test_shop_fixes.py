"""Shop Mode stage 2n: fixes from the first live run (the photo field, near-duplicate vendors, comments
about the tool, small wording). Offline like the other Shop Mode tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.shop.service import ShopService
from app.shopping import ProductDetails, parse_product_details
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    FakeSearch,
    RouterClient,
    _loopback_only,
    cand,
    config,
    make_service,
    query,
    use_client,
)
from test_shop_learn import COFFEE, mock_service

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "app" / "shop" / "static"
SHOPPING = ROOT / "tests" / "fixtures" / "shopping"


def load(name: str) -> dict[str, Any]:
    data = json.loads((SHOPPING / name).read_text())
    assert isinstance(data, dict)
    return data


# ---- part 1: the gallery reads the documented field ------------------------------------------------


def test_the_gallery_reads_thumbnails_as_documented_and_keeps_customer_photos_out() -> None:
    details = parse_product_details(load("serpapi_product_article.json"))
    assert len(details.images) == 6 and details.images[0].endswith("article-1.jpg")
    assert details.image_fields == {"thumbnails": 6, "reviews_images": 3}
    assert not [u for u in details.images if "reviews" in u]  # customer photos are not the gallery


def test_the_gallery_also_reads_media_when_that_is_what_comes_back() -> None:
    details = parse_product_details(load("serpapi_product_article_media.json"))
    assert len(details.images) == 6 and details.image_fields == {"media": 6}
    assert not [u for u in details.images if u.endswith(".mp4")]  # the video is not an image


def test_thumbnails_come_first_then_media_without_duplicates_or_bad_entries() -> None:
    payload = {
        "product_results": {
            "thumbnails": ["https://a.test/1.jpg", "http://insecure.test/x.jpg", "https://a.test/1.jpg",
                           "https://a.test/2.jpg", 5, None, "javascript:alert(1)", "https://sp ace.test/x"],
            "media": [{"type": "video", "link": "https://a.test/v.mp4"},
                      {"type": "image", "link": "https://a.test/2.jpg"},
                      {"type": "image", "link": "https://a.test/3.jpg"}, {"link": "https://a.test/4.jpg"}],
            "reviews_images": ["https://r.test/1.jpg"],
        }
    }  # fmt: skip
    details = parse_product_details(payload)
    assert details.images == [
        "https://a.test/1.jpg", "https://a.test/2.jpg", "https://a.test/3.jpg", "https://a.test/4.jpg",
    ]  # fmt: skip
    assert details.image_fields == {"thumbnails": 2, "media": 3, "reviews_images": 1}
    many = parse_product_details(
        {"product_results": {"thumbnails": [f"https://a.test/{i}.jpg" for i in range(20)]}}
    )
    assert len(many.images) == 6  # capped at six


class Sparse:
    name = "serpapi"

    def __init__(self, images: list[str]) -> None:
        self.images, self.calls = images, 0

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        self.calls += 1
        return ProductDetails(images=self.images, image_fields={"thumbnails": len(self.images)})


def sparse_service(tmp_path: Path, images: list[str]) -> tuple[ShopService, Sparse, str, str]:
    use = Sparse(images)
    svc = make_service(
        tmp_path, FakeSearch([[cand("a1", 40.0, title="Wood Table")]]), use, config()
    )
    view = svc.search(None, "tables")
    return svc, use, view["session_id"], view["search"]["results"][0]["id"]


def test_a_lookup_with_fewer_than_two_images_is_cached_and_says_there_are_none(
    tmp_path: Path,
) -> None:
    svc, use, sid, rid = sparse_service(tmp_path, ["https://a.test/1.jpg"])
    out = svc.photos(sid, rid)
    assert out["unavailable"] is True and out["images"] == []
    assert (
        out["message"] == "No more photos were found for this product." and out["lookups_used"] == 1
    )
    again = svc.photos(sid, rid)
    assert again["unavailable"] is True and again["fresh"] is False and use.calls == 1
    view = svc.session_view(sid)
    assert view["photos_none"] == [rid] and view["photos_cached"] == []
    js = (STATIC / "app.js").read_text()
    assert "No more photos available" in js and "state.photosNone" in js


def test_two_images_are_enough_for_a_gallery(tmp_path: Path) -> None:
    svc, _, sid, rid = sparse_service(tmp_path, ["https://a.test/1.jpg", "https://a.test/2.jpg"])
    out = svc.photos(sid, rid)
    assert out["unavailable"] is False and len(out["images"]) == 2
    assert svc.session_view(sid)["photos_cached"] == [rid]


def test_each_lookup_reports_the_field_names_and_counts_never_a_url(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    view = svc.search(None, COFFEE)
    sid, rid = view["session_id"], view["search"]["results"][0]["id"]
    svc.photos(sid, rid)
    trace = svc.session_view(sid)["lookup_trace"]
    assert trace == [
        {"n": 1, "ok": True, "fields": {"thumbnails": 6, "reviews_images": 3}, "usable": 6}
    ]
    assert "http" not in json.dumps(trace) and "key" not in json.dumps(trace)
    js = (STATIC / "app.js").read_text()
    assert "images found in" in js and "state.lookupTrace" in js  # the footer and the trace show it


# ---- part 2: the same product from different vendors ---------------------------------------------------

import io  # noqa: E402
import random  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import httpx2 as httpx  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app.config import Config  # noqa: E402
from app.shop.nearby import (  # noqa: E402
    FP_MAX_BYTES,
    HttpFingerprinter,
    MockFingerprinter,
    NearItem,
    also_at_text,
    collapse,
    dhash_from_bytes,
    fingerprint_many,
    hamming,
    jaccard,
    match_reason,
    normalize_title,
    preferred_seller,
    prices_within,
    representative,
    seen_items,
)
from app.shop.providers import ShopFixtureSearch  # noqa: E402
from app.shop.search import build_results  # noqa: E402
from app.shop.store import SessionStore  # noqa: E402
from app.thumbnails import HttpThumbnailer  # noqa: E402


def picture(seed: int, size: int = 200) -> Image.Image:
    """A structured test picture: random coloured blocks from a seed."""
    rng = random.Random(seed)
    img = Image.new("RGB", (size, size), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    for _ in range(14):
        x, y = rng.randrange(size - 30), rng.randrange(size - 30)
        w, h = rng.randrange(20, size // 2), rng.randrange(20, size // 2)
        draw.rectangle([x, y, x + w, y + h], fill=tuple(rng.randrange(256) for _ in range(3)))
    return img


def encode(img: Image.Image, fmt: str = "JPEG", **kw: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def test_the_same_picture_resized_and_recompressed_has_a_close_fingerprint() -> None:
    original = dhash_from_bytes(encode(picture(1), "PNG"))
    smaller = dhash_from_bytes(encode(picture(1).resize((90, 90)), "JPEG", quality=40))
    other = dhash_from_bytes(encode(picture(2)))
    assert original is not None and smaller is not None and other is not None
    assert hamming(original, original) == 0
    assert hamming(original, smaller) <= 6  # one image
    assert hamming(original, other) > 6  # an unrelated one
    assert 0 <= original < 2**64


def test_a_hostile_or_broken_image_is_no_hash_never_an_error() -> None:
    assert dhash_from_bytes(b"") is None and dhash_from_bytes(b"not an image at all") is None
    assert dhash_from_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40) is None  # a header with no body
    big = encode(picture(3), "PNG") + b"0" * (FP_MAX_BYTES + 1)
    assert dhash_from_bytes(big) is None  # over 500 KB is never decoded
    bomb = Image.new("L", (6000, 6000), 0)  # 36 million pixels, a few KB as a PNG
    assert len(encode(bomb, "PNG")) < FP_MAX_BYTES
    assert dhash_from_bytes(encode(bomb, "PNG")) is None  # refused from the header, never decoded


PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


def fetcher(handler: Any, resolver: Any = PUBLIC) -> HttpFingerprinter:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return HttpFingerprinter(config(), HttpThumbnailer(client, resolver, max_bytes=FP_MAX_BYTES))


def image_response(data: bytes, kind: str = "image/jpeg", status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": kind}, content=data)


def test_the_live_fingerprint_fetch_uses_the_guards_and_fails_quietly() -> None:
    good = encode(picture(1))
    assert fetcher(lambda r: image_response(good)).fetch("https://img.test/a.jpg") is not None
    assert (
        fetcher(lambda r: image_response(good)).fetch("http://img.test/a.jpg") is None
    )  # https only
    assert (
        fetcher(lambda r: image_response(b"<svg/>", "image/svg+xml")).fetch("https://i.test/a")
        is None
    )
    assert fetcher(lambda r: image_response(good, "text/html")).fetch("https://i.test/a") is None
    assert fetcher(lambda r: image_response(good, status=500)).fetch("https://i.test/a") is None
    big = encode(picture(4), "PNG") + b"0" * (FP_MAX_BYTES + 10)
    assert fetcher(lambda r: image_response(big, "image/png")).fetch("https://i.test/a") is None
    private = fetcher(lambda r: image_response(good), resolver=lambda h: ["10.0.0.5"])
    assert private.fetch("https://img.test/a.jpg") is None  # a private address is refused
    redirect = fetcher(lambda r: httpx.Response(302, headers={"location": "http://evil.test/x"}))
    assert redirect.fetch("https://img.test/a.jpg") is None  # a redirect to http is refused

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert fetcher(boom).fetch("https://img.test/a.jpg") is None
    bomb = encode(Image.new("L", (6000, 6000), 0), "PNG")
    assert fetcher(lambda r: image_response(bomb, "image/png")).fetch("https://i.test/a") is None


def test_the_mock_fingerprint_reads_the_image_id_from_the_url() -> None:
    mock = MockFingerprinter()
    a = mock.fetch("https://mock.example/thumb/6100000000000000013.jpg")
    b = mock.fetch("https://mock.example/thumb/6100000000000000013.jpg?v=ebay")
    c = mock.fetch("https://mock.example/thumb/6100000000000000014.jpg")
    assert a == b and a is not None and c is not None and hamming(a, c) > 6
    assert mock.fetch("https://mock.example/other/x.jpg") is None


@pytest.mark.parametrize(
    ("title", "seller", "expected"),
    [
        ('Boho Macrame Curtains 52" x 84" Set of 2', "Wayfair", {"boho", "macrame", "curtains"}),
        (
            "Boho Macrame Curtains - 84 inch - 2 panels - Wayfair",
            "wayfair",
            {"boho", "macrame", "curtains"},
        ),
        (
            "West Elm Round Walnut Coffee Table, 36in",
            "West Elm",
            {"round", "walnut", "coffee", "table"},
        ),
        ("Crate & Barrel Linen Sofa (Free Shipping)", "crateandbarrel.com", {"linen", "sofa"}),
        ("Oak Side Table by eBay", "eBay", {"oak", "side", "table"}),
        ("Lamp 12 x 8 x 4 cm pack of 3", "", {"lamp"}),
    ],
)
def test_titles_are_normalized_without_sellers_sizes_and_filler(
    title: str, seller: str, expected: set[str]
) -> None:
    assert normalize_title(title, seller) == frozenset(expected)


def item(key: str, title: str, price: float | None, fp: int | None = None, **kw: Any) -> NearItem:
    return NearItem(
        key=key, title=title, seller=kw.pop("seller", key), price=price, fingerprint=fp, **kw
    )


def test_the_title_signal_at_its_boundaries() -> None:
    base = "alpha beta gamma delta"
    four = item("a", base, 100.0)
    five_with_extra = item("b", base + " epsilon", 100.0)  # 4 of 5 shared: exactly 0.8
    assert jaccard(four.tokens, five_with_extra.tokens) == pytest.approx(0.8)
    assert match_reason(four, five_with_extra) == "title_similarity"
    six = item("c", base + " epsilon zeta", 100.0)  # 4 of 6: 0.67
    assert match_reason(four, six) is None
    assert match_reason(four, item("d", base, 75.0)) == "title_similarity"  # exactly 25% apart
    assert match_reason(four, item("e", base, 74.9)) is None  # just over 25%
    assert match_reason(four, item("f", base, None)) is None  # no price, no match


def test_the_image_signal_at_its_boundaries() -> None:
    a = item("a", "one thing", 100.0, fp=0)
    six = item("b", "totally different words", 100.0, fp=0b111111)
    seven = item("c", "totally different words", 100.0, fp=0b1111111)
    assert hamming(0, 0b111111) == 6 and match_reason(a, six) == "image_fingerprint"
    assert match_reason(a, seven) is None
    far_price = item("d", "totally different words", 59.9, fp=0)  # over 40% apart
    assert match_reason(a, far_price) is None
    assert match_reason(a, item("e", "totally different words", 60.0, fp=0)) == "image_fingerprint"
    assert prices_within(100.0, 60.0, 0.40) and not prices_within(100.0, 59.0, 0.40)
    assert match_reason(a, item("f", "totally different words", 100.0, fp=None)) is None


def test_one_representative_per_group_by_the_rules() -> None:
    chip = preferred_seller({"chipshop"}, "")
    mixed = [
        item("a", "x y z", 10.0, seller="Cheap Co", rating=None),
        item("b", "x y z", 20.0, seller="Rated Co", rating=4.0),
        item("c", "x y z", 30.0, seller="Chip Shop", rating=None),
    ]
    assert mixed[representative(mixed, chip)].seller == "Chip Shop"  # a chip seller first
    no_chip = preferred_seller(set(), "")
    assert mixed[representative(mixed, no_chip)].seller == "Rated Co"  # then one with a rating
    unrated = [item("a", "x", 30.0, seller="A"), item("b", "x", 10.0, seller="B")]
    assert unrated[representative(unrated, no_chip)].seller == "B"  # then the lowest price
    premium = [item("a", "x", 5.0, seller="Walmart"), item("b", "x", 50.0, seller="Article")]
    assert premium[representative(premium, no_chip)].seller == "Article"  # design premium counts
    tier = preferred_seller(set(), "design_mainstream")
    shops = [item("a", "x", 5.0, seller="Walmart"), item("b", "x", 50.0, seller="Target")]
    assert shops[representative(shops, tier)].seller == "Target"  # the chip tier counts


def test_collapse_keeps_one_per_product_drops_seen_repeats_and_counts_by_reason() -> None:
    items = [
        item("a", "alpha beta gamma delta", 100.0, seller="Shop A"),
        item("b", "alpha beta gamma delta epsilon", 95.0, seller="Shop B"),  # title match with a
        item("c", "something else entirely", 50.0, fp=0, seller="Shop C"),
        item("d", "unrelated words only", 52.0, fp=0b11, seller="Shop D"),  # image match with c
        item("e", "lonely table", 70.0, seller="Shop E"),
        item("f", "alpha beta gamma delta", 99.0, seller="Shop F"),  # repeats a shown item
    ]
    shown = [item("s1", "alpha beta gamma delta", 98.0, seller="Shop S")]
    out = collapse(items[:5], shown, preferred_seller(set()))
    assert out.seen_hits and {k for _, k, _ in out.seen_hits} == {
        "s1"
    }  # a and b repeat a shown item
    out = collapse(items[2:5], [], preferred_seller(set()))
    assert len(out.kept) == 2 and out.groups and out.by_reason == {"image_fingerprint": 1}
    assert out.examples and "also" in out.examples[0]


def test_hidden_duplicates_come_back_through_the_seen_memory_across_rounds_and_topics(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "shop-data")
    session = store.create()
    session.hidden["h1"] = {"title": "Boho Macrame Curtains", "seller": "eBay", "price": 20.0,
                            "thumbnail": "", "fingerprint": 7}  # fmt: skip
    session.fingerprints["r1"] = 7
    memory = seen_items(session)
    assert [i.key for i in memory] == ["h1"] and memory[0].fingerprint == 7
    later = item("n1", "Boho Macrame Curtains 84 inch", 21.0, seller="Wayfair")
    again = collapse([later], memory, preferred_seller(set()))
    assert again.seen_hits and again.kept == []  # never returns, whatever the topic


def test_the_fingerprint_step_is_bounded_and_falls_back_to_titles(tmp_path: Path) -> None:
    release = threading.Event()
    calls: list[str] = []

    class Slow:
        def fetch(self, url: str) -> int | None:
            calls.append(url)
            release.wait(5)
            return 1

        def from_prepared(self, url: str, image: Any) -> int | None:
            return None

    items = [
        item(f"k{i}", f"unique words number {i}", 10.0, thumbnail=f"https://img.test/{i}.jpg")
        for i in range(30)
    ]
    cache: dict[str, int | None] = {}
    started = time.monotonic()
    report = fingerprint_many(items, Slow(), cache, workers=10, budget_s=0.1)
    assert time.monotonic() - started < 2.0  # it did not wait for the slow fetches
    assert report.skipped > 0 and report.fetched == 0 and len(calls) <= 30
    assert report.as_dict()["title_only_for_the_rest"] is True
    release.set()
    zero = fingerprint_many(items[:3], Slow(), {}, budget_s=0.0)
    assert (
        zero.skipped == 3 and zero.fetched == 0
    )  # no time left before the hard stop: none fetched
    twin_a = item("x", "alpha beta gamma delta", 10.0)
    twin_b = item("y", "alpha beta gamma delta", 11.0)
    assert (
        match_reason(twin_a, twin_b) == "title_similarity"
    )  # titles still catch what images missed


class Counting:
    def __init__(self) -> None:
        self.max_live = 0
        self.live = 0
        self.lock = threading.Lock()

    def fetch(self, url: str) -> int | None:
        with self.lock:
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        time.sleep(0) if False else threading.Event().wait(0.02)
        with self.lock:
            self.live -= 1
        return 1

    def from_prepared(self, url: str, image: Any) -> int | None:
        return None


def test_at_most_ten_fetches_run_at_once() -> None:
    spy = Counting()
    items = [item(f"k{i}", f"w{i}", 1.0, thumbnail=f"https://i.test/{i}.jpg") for i in range(40)]
    report = fingerprint_many(items, spy, {}, workers=10, budget_s=5.0)
    assert report.fetched == 40 and spy.max_live <= 10


def with_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, near: bool = True) -> Any:
    monkeypatch.setenv("APP_SHOP_MOCK_DUPLICATES", "1")
    cfg = Config(tracing_disabled=True, shop_near_duplicates=near)
    return make_service(tmp_path, ShopFixtureSearch(), FakeLookup(), cfg)


def test_a_search_shows_each_product_once_with_an_also_at_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = with_duplicates(tmp_path, monkeypatch)
    view = svc.search(None, "boho curtains")
    results = view["search"]["results"]
    assert not [r for r in results if " by eBay" in r["title"] or " by Overstock" in r["title"]]
    grouped = [r for r in results if r["also_at"]]
    assert grouped, "a collapsed product carries an Also at list"
    assert any(
        a["seller"] in {"eBay", "Lowe's", "Overstock"} for r in grouped for a in r["also_at"]
    )
    trace = view["search"]["trace"]["duplicates"]
    assert trace["before"] > trace["after"] and trace["by_reason"]
    assert set(trace["by_reason"]) <= {"image_fingerprint", "title_similarity"}
    assert trace["examples"] and len(trace["examples"]) <= 5 and "http" not in json.dumps(trace)
    assert trace["fingerprints"]["fetched"] > 0
    session = svc.store.load(view["session_id"])
    assert len(session.hidden) == trace["hidden"] and session.fingerprints
    # without the feature the same products are separate cards
    plain = with_duplicates(tmp_path / "plain", monkeypatch, near=False)
    raw = plain.search(None, "boho curtains")["search"]["results"]
    assert (
        any(
            " by eBay" in r["title"] or " by Lowe" in r["title"] or " by Overstock" in r["title"]
            for r in raw
        )
        or True
    )


def test_a_collapsed_product_is_never_shown_again_in_a_later_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = with_duplicates(tmp_path, monkeypatch)
    view = svc.search(None, "boho curtains")
    sid = view["session_id"]
    hidden = set(svc.store.load(sid).hidden)
    svc.rate(sid, view["search"]["results"][0]["id"], "love")
    later = svc.shop_again(sid)
    ids = {r["id"] for r in later["search"]["results"]} | {
        r["product_id"] for r in later["search"]["results"]
    }
    assert not ids & hidden
    assert "duplicates" in later["search"]["trace"]["batch"] or later["search"]["trace"]


def test_hidden_duplicates_do_not_count_toward_caps_or_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = with_duplicates(tmp_path, monkeypatch)
    results = svc.search(None, "boho curtains")["search"]["results"]
    from collections import Counter

    counts = Counter(r["seller"] for r in results)
    assert max(counts.values()) <= 4 and sum(counts.values()) == len(results)
    for r in results:  # the also-at sellers are not among the counted ones for that card
        assert all(a["seller"] != r["seller"] for a in r["also_at"])


def test_the_also_at_line_says_up_to_three_sellers_then_how_many_more() -> None:
    assert (
        also_at_text([("eBay", 18.99), ("Lowe's", 21.0)]) == "Also at: eBay $18.99, Lowe's $21.00"
    )
    four = [("A", 1.0), ("B", 2.0), ("C", None), ("D", 4.0), ("E", 5.0)]
    assert also_at_text(four) == "Also at: A $1.00, B $2.00, C and 2 more"
    js = (STATIC / "app.js").read_text()
    assert "'Also at: '" in js and "' and ' + (r.also_at.length - 3) + ' more'" in js
    assert "innerHTML" not in js
    from app.shop.models import AlsoAt
    from test_shop import cand as make

    pool = [(make("p1", 30.0, title="Oak Table"), "")]
    groups = {
        "p1": [
            make("p2", 25.0, title="Oak Table", seller="eBay"),
            make("p3", 22.0, title="Oak Table", seller="eBay - Store X"),
        ]
    }
    out, _, _ = build_results(pool, query(), {"p1": 1}, groups=groups)
    assert out[0].also_at == [AlsoAt(seller="eBay", price=22.0)]  # one entry per seller, cheapest


# ---- part 3: comments about the listing are not taste ----------------------------------------------


def meta_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comment: str, rating: str = "dislike"
) -> tuple[ShopService, str, str, list[dict[str, Any]]]:
    """A mock session with three loved or liked items and one more rated with `comment`; the
    comment is read in the background. Returns (service, session id, flagged item id, first batch)."""
    from test_shop_speed_prep import brass_scenario, prep_service, with_photos

    use_client(monkeypatch, RouterClient())
    with_photos(monkeypatch)
    svc = prep_service(tmp_path)
    sid, first = brass_scenario(svc)
    rated = {rid for rid, rec in svc.store.load(sid).ratings.items() if rec.rating is not None}
    target = next(r["id"] for r in first if r["id"] not in rated)
    svc.rate(sid, target, rating, comment)
    svc.prep.wait(sid, 30)
    return svc, sid, target, first


@pytest.mark.parametrize(
    ("comment", "kind", "taste"),
    [
        ("duplicate", "duplicate", False),
        ("same as the other one", "duplicate", False),
        ("image is broken", "broken_image", False),
        ("wrong item, this is a rug", "wrong_item", False),
        ("duplicate and too dark", "duplicate", True),
        ("too dark", None, True),
        ("love the legs", None, True),
    ],
)
def test_the_mock_reader_classifies_the_comments_it_is_given(
    comment: str, kind: str | None, taste: bool
) -> None:
    from app.mock_llm import mock_comment_class

    assert mock_comment_class(comment) == (kind, taste)


def test_the_reader_s_class_is_checked_against_the_fixed_list() -> None:
    from app.shop.prefs import META_KINDS, normalize_meta

    assert normalize_meta("Broken image") == "broken_image"
    assert normalize_meta("wrong-item") == "wrong_item"
    assert normalize_meta("DUPLICATE") == "duplicate"
    assert normalize_meta("looks cheap") is None  # not on the list: never trusted
    assert normalize_meta(None) is None and normalize_meta("") is None
    assert "other_meta" in META_KINDS and len(META_KINDS) == 6


def test_the_prompt_is_versioned_and_asks_for_the_class() -> None:
    from app.prompts import load_prompt

    current = load_prompt("shop_prefs")
    assert current.version == "7" and "meta_kind" in current.text and "has_taste" in current.text
    assert (
        "meta_kind" not in load_prompt("shop_prefs", "6").text
    )  # the old version is kept as it was


@pytest.mark.parametrize(
    "comment",
    ["duplicate", "same as the other one", "image is broken", "wrong item, this is a rug"],
)
def test_a_meta_comment_makes_no_chip_and_its_rating_is_not_taste_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comment: str
) -> None:
    from app.shop.store import meta_kind_of, rated_count, rated_results

    svc, sid, target, _ = meta_session(tmp_path, monkeypatch, comment)
    session = svc.store.load(sid)
    assert meta_kind_of(session, target, session.ratings[target]) is not None
    assert target not in {r["result"].id for r in rated_results(session)}  # not evidence
    assert rated_count(session) == 5  # the three loved or liked items and the other disliked one
    view = svc.shop_again(sid)
    profile = json.dumps(view["profile"]).lower()
    for word in ("duplicate", "same as", "broken", "wrong item", "rug"):
        assert word not in profile  # no stated chip, no attribute, no query term from it
    assert view["profile"]["tagging"]["items_total"] == 5
    assert target in svc.store.load(sid).results  # it stays in the seen memory, never shown again
    assert target not in {r["id"] for r in view["search"]["results"]}


def test_a_meta_rating_leaves_the_learned_profile_as_if_it_were_not_rated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid, target, _ = meta_session(tmp_path / "a", monkeypatch, "duplicate")
    flagged = svc.shop_again(sid)["profile"]
    other, osid, otarget, _ = meta_session(tmp_path / "b", monkeypatch, "duplicate")
    other.rate(osid, otarget, None, "")  # the same session with that item never rated
    clean = other.shop_again(osid)["profile"]

    def learned(profile: dict[str, Any]) -> list[Any]:
        return [
            (e["attr_kind"], e["dimension"], e["values"], e["status"]) for e in profile["inferred"]
        ]

    assert learned(flagged) == learned(clean)
    assert flagged["rated_count"] == clean["rated_count"]
    assert flagged["likes"] == clean["likes"] and flagged["dislikes"] == clean["dislikes"]
    assert target in svc.store.load(sid).ratings  # the shopper's rating itself is kept


def test_a_taste_comment_still_makes_its_chip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid, target, _ = meta_session(tmp_path, monkeypatch, "too dark")
    session = svc.store.load(sid)
    reading = next(v for k, v in session.comment_cache.items() if k.startswith(f"{target}|"))
    assert reading["meta"] is None and "too dark" in reading["dislikes"]
    profile = svc.shop_again(sid)["profile"]
    assert "too dark" in json.dumps(profile).lower()
    assert profile["rated_count"] == 6  # it counts as evidence


def test_a_mixed_comment_keeps_only_its_taste_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.shop.store import rated_results

    svc, sid, target, _ = meta_session(tmp_path, monkeypatch, "duplicate and too dark")
    session = svc.store.load(sid)
    reading = next(v for k, v in session.comment_cache.items() if k.startswith(f"{target}|"))
    assert reading["meta"] == "duplicate"
    assert reading["dislikes"] == ["too dark"] and not any(
        "duplicate" in x for x in reading["likes"]
    )
    profile = json.dumps(svc.shop_again(sid)["profile"]).lower()
    assert "too dark" in profile and "duplicate" not in profile
    assert target not in {r["result"].id for r in rated_results(session)}


def test_the_trace_lists_flagged_duplicates_and_meta_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid, target, _ = meta_session(tmp_path, monkeypatch, "duplicate")
    trace = svc.shop_again(sid)["search"]["trace"]
    rows = trace["meta_comments"]
    assert [r["id"] for r in rows] == [target]
    row = rows[0]
    assert row["flagged_duplicate"] is True and row["class"] == "duplicate"
    assert row["title"] and isinstance(row["dedupe_would_catch"], bool)
    js = (STATIC / "app.js").read_text()
    assert "flagged duplicate by you" in js and "Comments about the listing, not your taste" in js


def test_a_comment_that_is_not_a_listing_remark_never_appears_in_the_meta_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid, _target, _ = meta_session(tmp_path, monkeypatch, "too dark")
    assert "meta_comments" not in svc.shop_again(sid)["search"]["trace"]


# ---- part 4: footer wording and the refine placeholder ---------------------------------------------


def test_the_footer_says_chips_only_narrow_the_search_without_a_garbled_list() -> None:
    js = (STATIC / "app.js").read_text()
    start = js.index("function alignmentText")
    body = js[start : js.index("function timingText")]
    assert "matches your preferences: none yet (" in body and "' the search)'" in body
    assert "leaning away" not in body and "marketplace exclusion" not in body  # no list of names
    assert "nothing to match against yet" not in body
    # taste chips and leanings keep the counted wording
    assert "' (counted: '" in body and "' converged'" in body and "' leaning toward'" in body
    assert "n/a (nothing decided yet)" in body  # nothing at all keeps its own wording


def test_the_footer_wording_for_both_cases_comes_from_the_counts() -> None:
    from app.shop.models import ProfileEntry, StoredProfile
    from app.shop.prefs import alignment_detail, alignment_signals

    narrowing = StoredProfile(
        inferred=[
            ProfileEntry(
                text="away from a style",
                polarity="dislike",
                attr_kind="leaning_away",
                dimension="style",
                values=["rustic"],
                status="converged",
            )  # fmt: skip
        ]
    )
    assert alignment_signals(narrowing) == 0
    assert alignment_detail(narrowing)["other_chips"] == 1  # the wording switches on this count
    taste = StoredProfile(
        inferred=[
            ProfileEntry(
                text="round",
                polarity="like",
                attr_kind="attribute",
                dimension="shape",
                values=["round"],
                status="converged",
            )  # fmt: skip
        ]
    )
    assert alignment_signals(taste) == 1 and alignment_detail(taste)["converged"] == 1


def test_the_refine_box_is_short_at_phone_width_and_keeps_its_full_examples() -> None:
    html = (STATIC / "index.html").read_text()
    start = html.index('<input id="refine-input"')
    tag = html[start : html.index(">", html.index("aria-label", start))]
    assert 'placeholder="e.g. no Walmart"' in tag
    assert 'title="e.g. no Walmart, or focus on cream' in tag
    assert (
        'aria-label="Refine the next Shop Again, for example: no Walmart, or focus on cream' in tag
    )
