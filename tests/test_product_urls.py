from __future__ import annotations

import pytest

from app.retrieval import is_product_page_url, product_page_rejection, verify_candidates
from app.schemas import Candidate

# The two URLs the first live run returned and the guard wrongly let through.
LIVE_RUN_LISTINGS = [
    "https://nathanjames.com/collections/tv-stands-entertainment-centers-media-consoles",
    "https://www.wayfair.com/keyword.php?keyword=minimalist+tv+stand",
]

PRODUCT_PAGES = [
    "https://www.ikea.com/us/en/p/kivik-sofa-with-chaise-tibbleby-beige-gray-s09483322/",
    "https://www.wayfair.com/furniture/pdp/mercury-row-aleah-sofa-w005871234.html",
    "https://www.amazon.com/Zinus-Upholstered-Sofa/dp/B07XYZ1234",
    "https://example-furniture.com/products/oslo-walnut-side-table",
    "https://www.target.com/p/threshold-sofa/-/A-12345678",
    # a Shopify product reached through a collection is still a product page
    "https://nathanjames.com/collections/side-tables/products/hailey-side-table",
]


@pytest.mark.parametrize("url", LIVE_RUN_LISTINGS)
def test_live_run_listing_urls_are_rejected(url: str) -> None:
    assert not is_product_page_url(url)
    assert product_page_rejection(url) is not None


@pytest.mark.parametrize("url", PRODUCT_PAGES)
def test_realistic_product_pages_pass(url: str) -> None:
    assert is_product_page_url(url), product_page_rejection(url)


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("https://shop.example/category/sofas", "listing_segment:category"),
        ("https://shop.example/categories/sofas/leather", "listing_segment:categories"),
        ("https://shop.example/search", "listing_segment:search"),
        ("https://shop.example/browse/living-room", "listing_segment:browse"),
        ("https://www.amazon.com/s/sofas", "listing_segment:s"),
        ("https://shop.example/c", "listing_segment:c"),
        ("https://shop.example/furniture/c/", "listing_segment:c"),
        ("https://www.ikea.com/us/en/cat/sofas-fu003/", "listing_segment:cat"),
        ("https://shop.example/", "no_path"),
        ("https://shop.example/lookup/keyword.php", "keyword.php"),
        ("https://shop.example/x?q=sofa", "search_query_key:q"),
        ("https://shop.example/x?Query=sofa", "search_query_key:query"),
        ("https://shop.example/x?search=sofa", "search_query_key:search"),
        ("https://shop.example/x?filter=color", "search_query_key:filter"),
    ],
)
def test_each_listing_rule_fires(url: str, rule: str) -> None:
    assert product_page_rejection(url) == rule


def test_c_segment_followed_by_an_item_is_not_a_listing() -> None:
    assert is_product_page_url("https://shop.example/c/grey-sofa-1234")


def test_unrelated_query_keys_do_not_reject() -> None:
    assert is_product_page_url("https://shop.example/products/sofa?variant=123&color=grey")


def _cand(url: str) -> Candidate:
    return Candidate(name="n", retailer="r", price=1.0, currency="USD", url=url)


def test_verify_requires_both_rules_and_labels_the_reason() -> None:
    listing, product, invented = LIVE_RUN_LISTINGS[0], PRODUCT_PAGES[0], "https://x.example/p/1"
    kept, dropped = verify_candidates(
        [_cand(listing), _cand(product), _cand(invented)], {listing, product}
    )
    assert [c.url for c in kept] == [product]
    assert {d["url"]: d["reason"] for d in dropped} == {
        listing: "not_a_product_page:listing_segment:collections",
        invented: "url_not_in_search_results",
    }


def test_membership_is_checked_before_page_type() -> None:
    """A listing URL that was never in the results is reported as the stronger problem."""
    _, dropped = verify_candidates([_cand(LIVE_RUN_LISTINGS[1])], set())
    assert dropped[0]["reason"] == "url_not_in_search_results"
