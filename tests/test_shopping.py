from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from app.shopping import (
    FIXTURE_DIR,
    FIXTURE_LOOKUPS,
    FixtureLookup,
    FixtureSearch,
    ProviderError,
    SerpApiProductLookup,
    SerpApiSearch,
    SerperSearch,
    Store,
    choose_store,
    parse_dimensions,
    parse_length_m,
    parse_price,
    parse_product_details,
    parse_serpapi_search,
    parse_serper_search,
    strip_tracking,
)

KEY = "sk-test-FAKE-KEY-1234567890"


def _fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURE_DIR / name).read_text())
    assert isinstance(data, dict)
    return data


# ---- search parsing ---------------------------------------------------------------------------


def test_serper_fixture_parses_including_an_item_with_no_link() -> None:
    results = parse_serper_search(_fixture("serper_search_oak_side_table.json"))
    assert len(results) == 8
    first = results[0]
    assert (first.title, first.seller, first.price, first.currency) == (
        "Article Wood C Side Table", "Article", 249.0, "USD",
    )  # fmt: skip
    assert first.product_id == "6965038339607673363"
    assert first.rating == 4.8 and first.rating_count == 57
    assert first.google_panel_url and first.google_panel_url.startswith("https://www.google.com/")
    no_link = [r for r in results if r.google_panel_url is None]
    assert len(no_link) == 1 and no_link[0].seller == "Walmart - Oubayajia"


def test_serpapi_fixture_parses_extracted_price_and_reviews() -> None:
    results = parse_serpapi_search(_fixture("serpapi_search_oak_side_table.json"))
    assert len(results) == 8
    ikea = results[0]
    assert ikea.title == "IKEA GUTTANE side table" and ikea.seller == "IKEA"
    assert ikea.price == pytest.approx(109.99) and ikea.rating == 4.7 and ikea.rating_count == 45
    assert ikea.product_id == "7810752583849448260"
    assert ikea.google_panel_url and ikea.google_panel_url.startswith("https://www.google.com/")


def test_both_providers_use_the_same_product_ids() -> None:
    a = {r.product_id for r in parse_serper_search(_fixture("serper_search_oak_side_table.json"))}
    b = {r.product_id for r in parse_serpapi_search(_fixture("serpapi_search_oak_side_table.json"))}
    assert a & b  # the live comparison found 30 of 40 shared; the trimmed fixtures keep overlap


def test_items_without_an_id_or_title_are_skipped() -> None:
    assert parse_serper_search({"shopping": [{"title": "x"}, {"productId": "1"}, {}]}) == []
    assert parse_serpapi_search({"shopping_results": [{"title": "x"}, {"product_id": "1"}]}) == []
    assert parse_serper_search({}) == [] and parse_serpapi_search({}) == []


# ---- price parsing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$249.00", (249.0, "USD")),
        ("$1,299.00", (1299.0, "USD")),
        ("$59", (59.0, "USD")),
        ("£1,000.50", (1000.5, "GBP")),
        ("€89.99", (89.99, "EUR")),
        ("$249-$249", (249.0, "USD")),
        ("From $10.50", (10.5, "USD")),
        (12.5, (12.5, "USD")),
        ("149", (149.0, "USD")),
    ],
)
def test_parse_price(text: Any, expected: tuple[float, str]) -> None:
    assert parse_price(text) == expected


@pytest.mark.parametrize("text", ["", "Free", "n/a", None, True, [], {}])
def test_parse_price_rejects_non_prices(text: Any) -> None:
    assert parse_price(text) is None


# ---- dimensions -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "metres"),
    [
        ("58 cm", 0.58),
        ("39 cm", 0.39),
        ('12 5/8 "', 0.3207),
        ("12 5/8 in", 0.3207),
        ('5/8"', 0.0159),
        ("12.5 inches", 0.3175),
        ("610 mm", 0.61),
        ("1.2 m", 1.2),
        ("58cm", 0.58),
        ("30 in.", 0.762),
    ],
)
def test_parse_length(value: str, metres: float) -> None:
    assert parse_length_m(value) == pytest.approx(metres, abs=1e-4)


@pytest.mark.parametrize(
    "value", ["58", "2 x 3 ft", "58 x 39 cm", "wide", "", "0 cm", "12 furlongs"]
)
def test_parse_length_rejects_ambiguous_or_unsupported(value: str) -> None:
    assert parse_length_m(value) is None


def test_ikea_fixture_dimensions() -> None:
    details = parse_product_details(_fixture("serpapi_product_ikea_guttane.json"))
    dims = details.dimensions
    assert dims is not None
    assert dims.width_m == pytest.approx(0.58) and dims.depth_m == pytest.approx(0.39)
    assert dims.height_m == pytest.approx(0.3207, abs=1e-4)
    assert dims.length_m is None


def test_ikea_values_from_features_directly() -> None:
    features = [
        {"title": "Material Type", "value": "Particleboard"},
        {"title": "Depth", "value": "39 cm"},
        {"title": "Height", "value": '12 5/8 "'},
        {"title": "Width", "value": "58 cm"},
    ]
    dims = parse_dimensions(features, None)
    assert dims is not None
    assert (dims.width_m, dims.depth_m) == (0.58, 0.39)
    assert dims.height_m == pytest.approx(0.3207, abs=1e-4)


def test_dimensions_from_product_attributes_and_precedence() -> None:
    attrs = [{"name": "Width", "value": "100 cm"}, {"name": "Length", "value": "2 m"}]
    feats = [{"title": "Width", "value": "90 cm"}]
    dims = parse_dimensions(feats, attrs)
    assert dims is not None and dims.width_m == 0.9 and dims.length_m == 2.0  # features win


def test_dimensions_are_none_when_absent_or_unparseable() -> None:
    assert parse_dimensions(None, None) is None
    assert parse_dimensions([{"title": "Seat Height", "value": "45 cm"}], None) is None
    assert parse_dimensions([{"title": "Width", "value": "58"}], None) is None
    assert parse_product_details(_fixture("serpapi_product_article.json")).dimensions is None


# ---- tracking params and store choice ---------------------------------------------------------


def test_tracking_parameters_are_stripped_and_others_kept() -> None:
    url = (
        "https://www.shop.example/p/sofa-1?variant=22&utm_source=google&utm_medium=cpc"
        "&srsltid=AfmBOo&gclid=abc&gad_source=1&gad_campaignid=9&fbclid=zzz&color=grey#reviews"
    )
    assert strip_tracking(url) == "https://www.shop.example/p/sofa-1?variant=22&color=grey#reviews"
    assert strip_tracking("https://a.example/p/1") == "https://a.example/p/1"
    assert strip_tracking("https://a.example/p/1?srsltid=x") == "https://a.example/p/1"


def test_article_fixture_url_is_cleaned() -> None:
    store = parse_product_details(_fixture("serpapi_product_article.json")).stores[0]
    cleaned = strip_tracking(store.url)
    assert cleaned.startswith("https://www.article.com/") and "srsltid" not in cleaned
    assert "utm_" not in cleaned


def _store(name: str, url: str = "https://x.example/p/1", **kw: Any) -> Store:
    return Store(name, url, kw.get("price"), kw.get("shipping"), kw.get("total"), None)


def test_store_chosen_by_seller_match_ignoring_case_punctuation_and_dot_com() -> None:
    stores = [
        _store("Wayfair", "https://w.example/p/1", total=100.0),
        _store("Crate & Barrel", "https://cb.example/p/1", total=300.0),
        _store("West Elm", "https://we.example/p/1", total=200.0),
    ]
    assert choose_store(stores, "crate&barrel.com") and (
        choose_store(stores, "crate&barrel.com") == stores[1]
    )
    assert choose_store(stores, "WEST ELM") == stores[2]
    assert choose_store(stores, "west-elm.com") == stores[2]


def test_store_match_by_containment_then_lowest_total() -> None:
    stores = [_store("Walmart", total=50.0, url="https://wm.example/p/1"),
              _store("Target", total=40.0, url="https://t.example/p/1")]  # fmt: skip
    assert choose_store(stores, "Walmart - Oubayajia") == stores[0]  # containment
    assert choose_store(stores, "Unknown Seller") == stores[1]  # lowest total
    assert choose_store(stores, "") == stores[1]


def test_short_store_names_cannot_match_by_containment() -> None:
    stores = [
        _store("A", "https://a.example/p/1", total=9.0),
        _store("Target", "https://t.example/p/1", total=5.0),
    ]
    assert choose_store(stores, "Unrelated") == stores[1]  # lowest total, not "A" inside the word


def test_lowest_total_falls_back_to_price_and_skips_urlless_stores() -> None:
    stores = [
        _store("A", url="", total=1.0),
        _store("B", url="https://b.example/p/1", price=30.0),
        _store("C", url="https://c.example/p/1", price=20.0),
    ]
    assert choose_store(stores, "none") == stores[2]
    assert choose_store([_store("A", url="")], "A") is None
    assert choose_store([], "A") is None


# ---- HTTP providers (mock transport: no network) -------------------------------------------


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=15.0)


def test_serper_request_shape_and_parsing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_fixture("serper_search_oak_side_table.json"))

    results = SerperSearch(KEY, _client(handler)).search("oak side table")
    (req,) = seen
    assert req.method == "POST" and str(req.url) == "https://google.serper.dev/shopping"
    assert req.headers["X-API-KEY"] == KEY and KEY not in str(req.url)
    assert json.loads(req.content) == {"q": "oak side table", "gl": "us", "hl": "en"}
    assert len(results) == 8


def test_serpapi_search_and_lookup_request_shapes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        engine = request.url.params["engine"]
        name = (
            "serpapi_search_oak_side_table.json"
            if engine == "google_shopping"
            else "serpapi_product_article.json"
        )
        return httpx.Response(200, json=_fixture(name))

    SerpApiSearch(KEY, _client(handler)).search("oak side table")
    details = SerpApiProductLookup(KEY, _client(handler)).lookup("6965038339607673363")
    assert [r.url.params["engine"] for r in seen] == ["google_shopping", "google_product"]
    assert seen[1].url.params["product_id"] == "6965038339607673363"
    assert all(r.url.params["gl"] == "us" and r.url.params["hl"] == "en" for r in seen)
    assert details.stores and details.stores[0].seller == "Article"
    assert details.stores[0].total == 308.0 and details.stores[0].shipping == 59.0
    assert details.stores[0].availability == "In stock online"


@pytest.mark.parametrize(
    ("status", "phrase"),
    [(401, "authentication"), (402, "payment"), (429, "rate limited"), (400, "HTTP 400")],
)
def test_client_errors_name_provider_and_status_but_not_the_key_or_url(
    status: int, phrase: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"nope {KEY}")  # a hostile body must not leak either

    for provider in (SerperSearch(KEY, _client(handler)), SerpApiSearch(KEY, _client(handler))):
        with pytest.raises(ProviderError) as info:
            provider.search("q")
        message = str(info.value)
        assert provider.name in message and str(status) in message and phrase in message
        assert KEY not in message and "http" not in message.lower().replace("http ", "")
        assert info.value.status == status and info.value.provider == provider.name


def test_one_retry_on_5xx_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=_fixture("serper_search_oak_side_table.json"))

    assert len(SerperSearch(KEY, _client(handler)).search("q")) == 8
    assert calls["n"] == 2


def test_two_5xx_responses_raise_after_exactly_one_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    with pytest.raises(ProviderError, match="HTTP 500"):
        SerpApiProductLookup(KEY, _client(handler)).lookup("1")
    assert calls["n"] == 2


def test_timeout_is_retried_once_and_never_leaks_the_url() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout(f"timed out for {request.url}", request=request)

    with pytest.raises(ProviderError) as info:
        SerpApiSearch(KEY, _client(handler)).search("q")
    assert calls["n"] == 2 and "timed out" in str(info.value)
    assert KEY not in str(info.value) and "serpapi.com" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_timeout_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, json={"shopping": []})

    assert SerperSearch(KEY, _client(handler)).search("q") == []


def test_bad_json_and_missing_key_and_repr() -> None:
    with pytest.raises(ProviderError, match="not valid JSON"):
        SerperSearch(KEY, _client(lambda r: httpx.Response(200, text="<html>"))).search("q")
    with pytest.raises(ProviderError, match="API key is not set"):
        SerperSearch("")
    assert KEY not in repr(SerperSearch(KEY, _client(lambda r: httpx.Response(200, json={}))))


# ---- fixture-backed fakes -----------------------------------------------------------------------


def test_fixture_search_is_keyed_by_query_and_labels_results() -> None:
    fake = FixtureSearch(
        "serper",
        "serper_search_oak_side_table.json",
        {"Special Query": "serper_search_oak_side_table.json"},
    )
    results = fake.search("anything")
    assert len(results) == 8 and all(r.title.startswith("[FIXTURE] ") for r in results)
    assert len(fake.search("special query")) == 8 and fake.queries == ["anything", "special query"]


def test_fixture_lookup_unknown_id_has_no_stores() -> None:
    fake = FixtureLookup(FIXTURE_LOOKUPS)
    assert fake.lookup("not-a-real-id").stores == []
    assert fake.lookup("6965038339607673363").stores


# ---- fixtures never contain keys ----------------------------------------------------------------


def test_fixtures_contain_no_keys_or_key_like_strings() -> None:
    pattern = re.compile(r"api_key|apikey|x-api-key|sk-ant|secret|token=|[A-Za-z0-9]{40,}", re.I)
    files = [p for p in Path(FIXTURE_DIR).iterdir() if p.is_file()]
    assert len(files) >= 5
    for path in files:
        match = pattern.search(path.read_text())
        assert match is None, f"{path.name}: {match and match.group()!r}"
