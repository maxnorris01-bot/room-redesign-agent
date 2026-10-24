"""The preference profile behind Shop Again.

One small-model call (prompt shop_prefs) reads the request, the rated results with their comments
and the existing profile, and returns the complete updated profile: short attribute phrases the
shopper likes and dislikes, and how to adjust the next search. Comments are read by the model, not
matched against fixed tags. Code then sanitizes the answer and enforces the rules the model must
not be trusted with:

  * entries the visitor deleted (`removed`) are never added back, in likes, dislikes or adjustments;
  * the category and the price floor are never changed; `price_max` may only be lowered, by at most
    `MAX_PRICE_CUT` of the typed cap, and only when the model gives a reason;
  * every list is bounded and every phrase is short, lowercase text.

`apply_adjustments` builds the revised `ShopQuery`; `profile_summary` writes the plain-language line
shown on the page (no model call).

Prompt v2 adds what the shopper did not say: attributes inferred from the rated items themselves
(their titles, prices, sellers and, when they can be fetched, thumbnails). The model proposes; code
decides what stands (see `validate_inferred`): evidence from at least two items unless a comment
backs it, a seller affinity and a price band computed from the ratings (never the model's say-so),
style terms that only the request parser guessed marked as guesses and dropped when a loved or
liked item contradicts them, and deleted chips never coming back.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.images import PreparedImage
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import SchemaError, parse_model, to_output_schema
from app.shop.models import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_TERM_CHARS,
    MAX_TERMS,
    ImageReport,
    InferredAttribute,
    PreferenceProfile,
    PreferenceProfileV2,
    ProfileEntry,
    QueryAdjustments,
    ShopQuery,
    ShopResult,
    StoredProfile,
)
from app.shop.parse import strip_price_words
from app.shop.store import Session, now_iso, rated_results
from app.shopping_retrieval import seller_key
from app.thumbnails import Thumbnailer, fetch_traced

MAX_PROFILE_TERMS = 12
MAX_PRICE_CUT = 0.20  # price_max may fall by at most this fraction of the typed cap. A guess.
MAX_RATED_IN_PROMPT = 60
PREFS_MAX_TOKENS = 2000
MAX_IMAGES = {"love": 6, "like": 6, "dislike": 4}  # thumbnails offered to the model, by rating
IMAGE_WORKERS = 8
MAX_INFERRED = 10
LOVE_WEIGHT, LIKE_WEIGHT = 3, 1  # how much a loved and a liked item count as evidence
MIN_EVIDENCE_ITEMS = 2  # unless a comment backs the attribute (see `validate_inferred`)
MIN_AFFINITY_ITEMS = 2  # loved or liked items from one seller, with no dislike of it
UPPER_PERCENTILE, LOWER_PERCENTILE = (
    0.67,
    0.33,
)  # price band edges within the shown prices. A guess.
MIN_SHOWN_PRICES = 8  # fewest shown prices before a price band is worked out
MAX_QUERIES = 3
STOP_WORDS = frozenset({"with", "and", "the", "for", "from", "that", "this", "piece", "pieces"})
MIN_CONTAIN_CHARS = 4  # a shorter phrase only matches a removed entry exactly


def normalize_phrase(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " -'" else " " for c in text.lower())
    return " ".join(cleaned.split())[:MAX_TERM_CHARS].strip(" -'")


def phrases_match(a: str, b: str) -> bool:
    """Same phrase, or one contains the other (both at least `MIN_CONTAIN_CHARS` long)."""
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= MIN_CONTAIN_CHARS and shorter in longer


def is_removed(phrase: str, removed: list[str]) -> bool:
    key = normalize_phrase(phrase)
    return bool(key) and any(phrases_match(key, r) for r in removed)


def clean_phrases(
    values: list[str], removed: list[str], limit: int = MAX_PROFILE_TERMS
) -> list[str]:
    """Normalized, deduplicated phrases without removed entries, at most `limit`."""
    out: list[str] = []
    for raw in values:
        key = normalize_phrase(raw)
        if key and key not in out and not is_removed(key, removed):
            out.append(key)
    return out[:limit]


def clamp_price_max(base_max: float | None, proposed: float | None) -> float | None:
    """The new price cap, or None for "no change". Needs a typed cap to lower; never raises it and
    never cuts more than `MAX_PRICE_CUT` of it."""
    if base_max is None or proposed is None or proposed <= 0 or proposed != proposed:
        return None
    floor = round(base_max * (1 - MAX_PRICE_CUT), 2)
    new = round(max(proposed, floor), 2)
    return new if new < base_max else None


def merge_profile(
    existing: StoredProfile | None,
    proposed: PreferenceProfile | None,
    base: ShopQuery,
    rated_count: int,
) -> StoredProfile:
    """The stored profile after one preference call. `proposed=None` (the call failed) keeps the
    existing profile as it was. The deleted-entries list only ever grows here."""
    previous = existing or StoredProfile()
    if proposed is None:
        return previous
    removed = previous.removed
    adj = proposed.query_adjustments
    price_max = clamp_price_max(base.price_max, adj.price_max)
    note = ""
    if price_max is not None:
        note = " ".join(adj.price_note.split())[:120] or (
            f"Lowered the price limit to ${price_max:,.0f} (at most {MAX_PRICE_CUT:.0%} below "
            f"${base.price_max:,.0f}) because a comment asked for it."
            if base.price_max is not None
            else ""
        )
    adjustments = QueryAdjustments(
        search_text=strip_price_words(" ".join(adj.search_text.split()))[:MAX_SEARCH_TEXT_CHARS],
        style_terms=clean_phrases(adj.style_terms, removed, MAX_TERMS),
        material_terms=clean_phrases(adj.material_terms, removed, MAX_TERMS),
        must_have=clean_phrases(adj.must_have, removed, MAX_TERMS),
        avoid=clean_phrases(adj.avoid, removed, MAX_TERMS),
        price_max=price_max,
        price_note=note,
    )
    if is_removed(adjustments.search_text, removed):
        adjustments = adjustments.model_copy(update={"search_text": ""})
    return StoredProfile(
        likes=clean_phrases(proposed.likes, removed),
        dislikes=clean_phrases(proposed.dislikes, removed),
        adjustments=adjustments,
        removed=list(removed),
        updated_at=now_iso(),
        rated_count=rated_count,
    )


def sig_words(text: str) -> list[str]:
    """The words of a phrase that carry meaning: three letters or more, not filler."""
    return [
        w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOP_WORDS
    ]


def text_matches(phrase: str, text: str) -> bool:
    """Whether `text` (a title) shows `phrase`: all its significant words, or both when it has more
    than two. "brushed brass" needs both words; "walnut" needs the one."""
    words = sig_words(phrase)
    if not words:
        return False
    lowered = text.lower()
    return sum(1 for w in words if w in lowered) >= min(2, len(words))


def mentions(phrase: str, comment: str) -> bool:
    """Whether a comment names (any significant word of) the phrase."""
    lowered = comment.lower()
    return any(w in lowered for w in sig_words(phrase))


@dataclass(frozen=True)
class Rated:
    """A rated result as the inference rules see it."""

    id: str
    rating: str  # love | like | dislike
    comment: str
    title: str
    price: float | None
    seller: str
    thumbnail_url: str


def rated_map(session: Session) -> dict[str, Rated]:
    out: dict[str, Rated] = {}
    for row in rated_results(session):
        r: ShopResult = row["result"]
        out[r.id] = Rated(
            r.id, row["rating"], row["comment"], r.title, r.price, r.seller, r.thumbnail_url
        )
    return out


# ---- guesses: style terms only the parser supplied ----


def guess_style_terms(base: ShopQuery) -> list[str]:
    """Style terms the request parser produced that the shopper's own words do not contain.

    A term is the shopper's when every significant word of it (or a word sharing its first five
    letters) appears in the typed request. "industrial" for "coffee tables with a mix of wood and
    metal" is a guess; "boho" for "boho curtains" is not.
    """
    request_words = re.findall(r"[a-z0-9]+", base.request.lower())

    def typed(term: str) -> bool:
        words = sig_words(term)
        return bool(words) and all(
            any(rw == w or (len(w) >= 5 and rw[:5] == w[:5]) for rw in request_words) for w in words
        )

    return [t for t in base.style_terms if not typed(t)]


# ---- inferred attributes: what code accepts of the model's proposals ----


def _short(text: str) -> str:
    return normalize_phrase(text)


def _titles(rated: dict[str, Rated], ids: list[str]) -> list[str]:
    return [" ".join(rated[i].title.split())[:100] for i in ids[:3] if i in rated]


def validate_inferred(
    attrs: list[InferredAttribute],
    rated: dict[str, Rated],
    removed: list[str],
    image_ids: set[str],
    upper_band: bool = False,
) -> list[ProfileEntry]:
    """The model's attribute proposals that pass the rules, as stored entries.

    Seller and price-band proposals are ignored (code works those out: `seller_affinity`,
    `price_band_entry`). For the rest:

      * evidence ids must be rated results, loved or liked for a like, disliked for a dislike;
      * at least two evidence items, unless a comment on an evidence item names the attribute, or
        the one item is loved and has a comment;
      * a like needs a positive net weight (loved 3, liked 1, minus one per disliked result whose
        title shows it); a dislike is dropped when a loved or liked title shows it, unless a comment
        backs it;
      * a quality cue must be visible: in an evidence title, in an image that was actually sent, or
        be about the price band when an upper band exists;
      * deleted phrases never come back; a phrase is stored once.
    """
    out: list[ProfileEntry] = []
    seen: set[str] = set()
    for a in attrs:
        if a.kind in ("seller", "price_band"):
            continue
        text = _short(a.text)
        if not text or text in seen or is_removed(text, removed):
            continue
        want = ("love", "like") if a.polarity == "like" else ("dislike",)
        ids = [i for i in dict.fromkeys(a.evidence) if i in rated and rated[i].rating in want]
        if not ids:
            continue
        comment_backed = any(mentions(text, rated[i].comment) for i in ids)
        single_loved = (
            len(ids) == 1 and rated[ids[0]].rating == "love" and bool(rated[ids[0]].comment)
        )
        if len(ids) < MIN_EVIDENCE_ITEMS and not (comment_backed or single_loved):
            continue
        if a.polarity == "like":
            weight = sum(LOVE_WEIGHT if rated[i].rating == "love" else LIKE_WEIGHT for i in ids)
            against = sum(
                1 for r in rated.values() if r.rating == "dislike" and text_matches(text, r.title)
            )
            if weight - against <= 0:
                continue
        else:
            weight = len(ids)
            if not comment_backed and any(
                r.rating in ("love", "like") and text_matches(text, r.title) for r in rated.values()
            ):
                continue
        if a.kind == "quality_cue":
            visible = (
                any(text_matches(text, rated[i].title) for i in ids)
                or any(i in image_ids for i in ids)
                or ("price" in text and upper_band)
            )
            if not visible:
                continue
        seen.add(text)
        out.append(
            ProfileEntry(
                text=text,
                attr_kind=a.kind,
                polarity=a.polarity,
                evidence=ids[:6],
                evidence_titles=_titles(rated, ids),
                confidence=round(min(1.0, max(0.0, a.confidence)), 2),
                weight=weight,
            )
        )
    out.sort(key=lambda e: (e.polarity != "like", -e.weight))
    return out[:MAX_INFERRED]


def seller_affinity(rated: dict[str, Rated], removed: list[str]) -> ProfileEntry | None:
    """A seller with at least two loved or liked items and no dislike: an inferred like.

    Worked out from the ratings alone. A seller the shopper disliked anything from never qualifies.
    """
    groups: dict[str, list[Rated]] = {}
    for r in rated.values():
        if r.seller:
            groups.setdefault(seller_key(r.seller), []).append(r)
    best: tuple[int, str, list[Rated]] | None = None
    for key, items in groups.items():
        positive = [r for r in items if r.rating in ("love", "like")]
        if len(positive) < MIN_AFFINITY_ITEMS or any(r.rating == "dislike" for r in items):
            continue
        weight = sum(LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT for r in positive)
        if best is None or weight > best[0]:
            best = (weight, key, positive)
    if best is None:
        return None
    weight, _, positive = best
    name = " ".join(positive[0].seller.split())[:40]
    text = f"seller: {name}"
    if is_removed(text, removed):
        return None
    ids = [r.id for r in positive]
    return ProfileEntry(
        text=text, attr_kind="seller", polarity="like", evidence=ids[:6],
        evidence_titles=_titles(rated, ids), confidence=1.0, weight=weight,
    )  # fmt: skip


def seller_name(entry: ProfileEntry) -> str:
    return entry.text.removeprefix("seller:").strip()


def _percentile(sorted_values: list[float], q: float) -> float:
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _rank_fraction(sorted_values: list[float], value: float) -> float:
    below = sum(1 for v in sorted_values if v < value)
    equal = sum(1 for v in sorted_values if v == value)
    return (below + 0.5 * equal) / len(sorted_values)


def _round_money(lo: float, hi: float) -> tuple[float, float]:
    step = 50.0 if hi >= 300 else 10.0
    return math.floor(lo / step) * step, math.ceil(hi / step) * step


def price_band_entry(
    session: Session, rated: dict[str, Rated], removed: list[str]
) -> ProfileEntry | None:
    """Where the loved and liked items sit among every price the shopper was shown, in code.

    Needs two loved or liked items with a price and at least `MIN_SHOWN_PRICES` shown prices. An
    average position in the top third is "upper range", in the bottom third "lower range", the
    middle gives nothing. The bounds are that third of the shown prices. It is a preference signal
    for ranking and the summary line only: it never changes the typed price filter.
    """
    shown = sorted(r.price for r in session.results.values() if r.price is not None and r.price > 0)
    positive = [r for r in rated.values() if r.rating in ("love", "like") and r.price]
    if len(positive) < MIN_AFFINITY_ITEMS or len(shown) < MIN_SHOWN_PRICES:
        return None
    mean = sum(_rank_fraction(shown, r.price or 0.0) for r in positive) / len(positive)
    if mean >= UPPER_PERCENTILE:
        band, lo, hi = "upper", _percentile(shown, UPPER_PERCENTILE), shown[-1]
    elif mean <= LOWER_PERCENTILE:
        band, lo, hi = "lower", shown[0], _percentile(shown, LOWER_PERCENTILE)
    else:
        return None
    lo, hi = _round_money(lo, hi)
    text = f"{band} range, about ${lo:,.0f} to ${hi:,.0f}"
    if is_removed(text, removed) or is_removed(f"{band} price range", removed):
        return None
    ids = [r.id for r in positive]
    return ProfileEntry(
        text=text, attr_kind="price_band", polarity="like", evidence=ids[:6],
        evidence_titles=_titles(rated, ids), confidence=round(mean, 2), weight=len(positive),
        low=lo, high=hi, band=band,
    )  # fmt: skip


def merge_profile_v2(
    existing: StoredProfile | None,
    proposed: PreferenceProfileV2 | None,
    session: Session,
    base: ShopQuery,
    images: ImageReport,
    image_ids: set[str],
) -> StoredProfile:
    """The stored profile after one v2 preference call. `proposed=None` keeps the earlier profile
    (with this call's image report). Everything the model proposed is checked here."""
    previous = existing or StoredProfile()
    rated_count = sum(1 for rec in session.ratings.values() if rec.rating is not None)
    if proposed is None:
        return previous.model_copy(update={"images": images})
    stated = merge_profile(
        previous,
        PreferenceProfile(
            likes=proposed.likes,
            dislikes=proposed.dislikes,
            query_adjustments=proposed.query_adjustments,
        ),
        base,
        rated_count,
    )
    removed = stated.removed
    rated = rated_map(session)
    band = price_band_entry(session, rated, removed)
    inferred = validate_inferred(
        proposed.inferred,
        rated,
        removed,
        image_ids,
        upper_band=band is not None and band.band == "upper",
    )
    inferred = [
        e
        for e in inferred
        if not (e.polarity == "like" and e.text in stated.likes)
        and not (e.polarity == "dislike" and e.text in stated.dislikes)
    ]  # a phrase the shopper said is shown once, as theirs
    for extra in (seller_affinity(rated, removed), band):
        if extra is not None:
            inferred.append(extra)
    # guesses: the parser's own style terms, dropped when a loved or liked item contradicts them
    contradicted: list[str] = []
    for c in proposed.contradicted_guesses:
        key = _short(c.text)
        ids = [i for i in c.evidence if i in rated and rated[i].rating in ("love", "like")]
        positive_titles = [r.title for r in rated.values() if r.rating in ("love", "like")]
        if key and ids and not any(text_matches(key, t) for t in positive_titles):
            contradicted.append(key)
    guesses: list[str] = []
    dropped: list[str] = list(previous.dropped_guesses)
    for g in guess_style_terms(base):
        key = _short(g)
        if is_removed(key, removed):
            continue
        if any(phrases_match(key, c) for c in contradicted):
            if key not in dropped:
                dropped.append(key)
            continue
        if key in dropped:
            continue
        guesses.append(key)
    return stated.model_copy(
        update={
            "inferred": inferred,
            "guesses": guesses,
            "dropped_guesses": dropped,
            "images": images,
        }
    )


# ---- editing the profile ----


def remove_entry(profile: StoredProfile, kind: str, phrase: str) -> StoredProfile | None:
    """The profile without one chip, its phrase added to the deleted list; None if the chip is not
    there. `kind` is like or dislike (stated), inferred, or guess. Adjustment terms that match the
    phrase go too, so the next search drops them."""
    key = _short(phrase)
    update: dict[str, object] = {}
    if kind in ("like", "dislike"):
        entries = profile.likes if kind == "like" else profile.dislikes
        if key not in entries:
            return None
        update["likes" if kind == "like" else "dislikes"] = [x for x in entries if x != key]
    elif kind == "inferred":
        kept = [e for e in profile.inferred if _short(e.text) != key]
        if len(kept) == len(profile.inferred):
            return None
        update["inferred"] = kept
    elif kind == "guess":
        if key not in profile.guesses:
            return None
        update["guesses"] = [g for g in profile.guesses if g != key]
    else:
        return None
    removed = [*profile.removed, key] if key not in profile.removed else list(profile.removed)
    adj = profile.adjustments

    def drop(values: list[str]) -> list[str]:
        return [v for v in values if not phrases_match(v, key)]

    update["removed"] = removed[-50:]
    update["adjustments"] = adj.model_copy(
        update={
            "style_terms": drop(adj.style_terms),
            "material_terms": drop(adj.material_terms),
            "must_have": drop(adj.must_have),
            "avoid": drop(adj.avoid),
            "search_text": "" if phrases_match(adj.search_text, key) else adj.search_text,
        }
    )
    return profile.model_copy(update=update)


def apply_adjustments(base: ShopQuery, profile: StoredProfile | None) -> ShopQuery:
    """The query for a Shop Again: the typed query plus the profile's adjustments.

    Category, category key and the price floor are the typed query's; `price_max` is lowered only
    by an accepted adjustment. Terms are added after the typed ones, without repeats. A parser
    guess that was contradicted or deleted is taken out of the style terms.
    """
    if profile is None:
        return base
    adj = profile.adjustments

    def union(first: list[str], extra: list[str]) -> list[str]:
        return [*first, *[t for t in extra if t not in first]][:MAX_TERMS]

    price_max = base.price_max
    if adj.price_max is not None and (price_max is None or adj.price_max < price_max):
        price_max = clamp_price_max(base.price_max, adj.price_max) or price_max
    gone = [*profile.dropped_guesses, *profile.removed]
    styles = [
        t for t in base.style_terms if not (t in guess_style_terms(base) and is_removed_in(t, gone))
    ]
    return base.model_copy(
        update={
            "search_text": adj.search_text or base.search_text,
            "style_terms": union(styles, adj.style_terms),
            "material_terms": union(base.material_terms, adj.material_terms),
            "must_have": union(base.must_have, adj.must_have),
            "avoid": union(base.avoid, adj.avoid),
            "price_max": price_max,
        }
    )


def is_removed_in(phrase: str, gone: list[str]) -> bool:
    key = _short(phrase)
    return bool(key) and any(phrases_match(key, g) for g in gone)


def reapply_removed(profile: StoredProfile, removed: list[str]) -> StoredProfile:
    """The profile with a newer deleted list enforced again, for chips deleted while a Shop Again
    was running, so nothing deleted comes back."""
    adj = profile.adjustments

    def keep(values: list[str]) -> list[str]:
        return [v for v in values if not is_removed(v, removed)]

    return profile.model_copy(
        update={
            "removed": list(removed),
            "likes": keep(profile.likes),
            "dislikes": keep(profile.dislikes),
            "inferred": [e for e in profile.inferred if not is_removed(e.text, removed)],
            "guesses": keep(profile.guesses),
            "adjustments": adj.model_copy(
                update={
                    "style_terms": keep(adj.style_terms),
                    "material_terms": keep(adj.material_terms),
                    "must_have": keep(adj.must_have),
                    "avoid": keep(adj.avoid),
                    "search_text": "" if is_removed(adj.search_text, removed) else adj.search_text,
                }
            ),
        }
    )


# ---- queries for a wider pool ----


def _product_noun(base: ShopQuery) -> str:
    noun = base.category or " ".join(strip_price_words(base.search_text).split()[:3])
    return noun.strip()


def build_queries(base: ShopQuery, profile: StoredProfile, adjusted: ShopQuery) -> list[str]:
    """Up to three search phrasings for a Shop Again, in order:

      1. the typed query as adjusted by the profile;
      2. the product plus the strongest inferred material, finish and shape likes;
      3. when a seller affinity exists, the seller's name plus the product and its first must-have.

    Duplicates (after normalizing) are dropped, so a profile with nothing inferred gives one phrase.
    """
    product = _product_noun(base)
    out: list[str] = [adjusted.search_text]
    likes = [
        e
        for e in profile.inferred
        if e.polarity == "like" and e.attr_kind in ("material", "color_finish", "shape")
    ]
    likes.sort(key=lambda e: -e.weight)
    if likes and product:
        words = " ".join(e.text for e in likes[:3])
        out.append(f"{product} {words}")
    seller = next((e for e in profile.inferred if e.attr_kind == "seller"), None)
    if seller is not None and product:
        extra = adjusted.must_have[0] if adjusted.must_have else ""
        out.append(f"{seller_name(seller)} {product} {extra}")
    seen: set[str] = set()
    queries: list[str] = []
    for q in out:
        text = " ".join(strip_price_words(q).split())[:MAX_SEARCH_TEXT_CHARS]
        key = normalize_phrase(text)
        if text and key not in seen:
            seen.add(key)
            queries.append(text)
    return queries[:MAX_QUERIES]


# ---- the summary line and the alignment measure ----


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def profile_summary(profile: StoredProfile | None) -> str:
    """A plain sentence for the page, from the profile alone (no model call):

    "Looking for more brushed brass and wood and metal pieces from West Elm in the upper price
    range, fewer too dark." Stated and inferred likes are joined; a seller affinity and a price
    band add "from <seller>" and "in the <upper|lower> price range". Empty when there is nothing.
    """
    if profile is None:
        return ""
    inferred_likes = [
        e
        for e in profile.inferred
        if e.polarity == "like" and e.attr_kind not in ("seller", "price_band")
    ]
    likes = list(dict.fromkeys([*profile.likes, *[e.text for e in inferred_likes]]))[:4]
    dislikes = list(
        dict.fromkeys(
            [*profile.dislikes, *[e.text for e in profile.inferred if e.polarity == "dislike"]]
        )
    )[:4]
    seller = next((e for e in profile.inferred if e.attr_kind == "seller"), None)
    band = next((e for e in profile.inferred if e.attr_kind == "price_band"), None)
    parts: list[str] = []
    if likes or seller or band:
        text = f"more {_join(likes)}" if likes else "more pieces"
        if likes and (seller or band):
            text += " pieces"
        if seller:
            text += f" from {seller_name(seller)}"
        if band:
            text += f" in the {band.band} price range"
        parts.append(text)
    if dislikes:
        parts.append(f"fewer {_join(dislikes)}")
    text = f"Looking for {', '.join(parts)}." if parts else ""
    if profile.adjustments.price_note:
        text = f"{text} {profile.adjustments.price_note}".strip()
    return text


def alignment_counts(results: list[ShopResult], profile: StoredProfile | None) -> tuple[int, int]:
    """(results that match, results): a match is at least one inferred like and no dislike.

    A result matches an inferred like when its title shows the phrase (`text_matches`), it comes
    from the affinity seller, or its price is inside the price band. A result matches a dislike
    when its title shows an inferred or stated dislike. Guess chips and stated likes do not count.
    A profile with no inferred like matches nothing.
    """
    if not results or profile is None:
        return 0, len(results)
    likes = [e for e in profile.inferred if e.polarity == "like"]
    if not likes:
        return 0, len(results)
    dislikes = [*[e.text for e in profile.inferred if e.polarity == "dislike"], *profile.dislikes]

    def matches_like(r: ShopResult) -> bool:
        for e in likes:
            if e.attr_kind == "seller":
                if seller_key(r.seller) == seller_key(seller_name(e)):
                    return True
            elif e.attr_kind == "price_band":
                if r.price is not None and e.low is not None and e.high is not None:
                    if e.low <= r.price <= e.high:
                        return True
            elif text_matches(e.text, r.title):
                return True
        return False

    hits = sum(
        1
        for r in results
        if matches_like(r) and not any(text_matches(d, r.title) for d in dislikes)
    )
    return hits, len(results)


def preference_alignment(results: list[ShopResult], profile: StoredProfile | None) -> float:
    """`alignment_counts` as a fraction (0.0 for an empty list). A simple diagnostic for the stage 3
    eval: how much of a result list the inferred preferences would pick, not a quality gate."""
    hits, total = alignment_counts(results, profile)
    return round(hits / total, 4) if total else 0.0


# ---- the model call ----


def _money(value: float | None) -> str:
    return "no price" if value is None else f"${value:,.0f}"


def _flat(text: str, limit: int) -> str:
    return " ".join(text.replace("|", "/").split())[:limit]


def prefs_user_text(
    session: Session, base: ShopQuery, image_numbers: dict[str, int] | None = None
) -> str:
    """The v2 preference call's text. Each rated item is one line with its id (cited as
    evidence) and, when a thumbnail is attached, its image number (images come first, in order)."""
    profile = session.profile or StoredProfile()
    adj = profile.adjustments
    numbers = image_numbers or {}
    guesses = guess_style_terms(base)
    typed_styles = [t for t in base.style_terms if t not in guesses]
    price = "none" if base.price_max is None else f"up to {_money(base.price_max)}"
    stated_inferred = [f"{e.text} ({e.attr_kind}, {e.polarity})" for e in profile.inferred]
    lines = [
        f"Original request: {base.request}",
        f"Parsed query: search_text={base.search_text}; category={base.category or 'unknown'}; "
        f"price={price}; materials={', '.join(base.material_terms) or 'none'}; "
        f"must_have={', '.join(base.must_have) or 'none'}; avoid={', '.join(base.avoid) or 'none'}",
        f"Style words in the request: {', '.join(typed_styles) or 'none'}",
        "Style guessed by the request parser (not the shopper's words): "
        f"{', '.join(guesses) or 'none'}",
        "Existing profile:",
        f"  likes: {'; '.join(profile.likes) or 'none'}",
        f"  dislikes: {'; '.join(profile.dislikes) or 'none'}",
        f"  inferred: {'; '.join(stated_inferred) or 'none'}",
        f"  adjustments: search_text={adj.search_text or 'none'}; "
        f"style={', '.join(adj.style_terms) or 'none'}; "
        f"materials={', '.join(adj.material_terms) or 'none'}; "
        f"must_have={', '.join(adj.must_have) or 'none'}; avoid={', '.join(adj.avoid) or 'none'}",
        f"Removed entries (never bring these back): {'; '.join(profile.removed) or 'none'}",
        "",
        "Rated results (loved first; image N is the Nth attached image, none means title only):",
    ]
    rows = rated_results(session)
    order = {"love": 0, "like": 1, "dislike": 2}
    rows.sort(key=lambda r: order[r["rating"]])  # stable: newest first within a rating
    for row in rows[:MAX_RATED_IN_PROMPT]:
        r: ShopResult = row["result"]
        comment = _flat(row["comment"], 200) or "none"
        image = f"image {numbers[r.id]}" if r.id in numbers else "image none"
        lines.append(
            f"- {row['rating']} | id: {r.id} | {_flat(r.title, 100)} | {_money(r.price)} | "
            f"{_flat(r.seller, 40) or '?'} | comment: {comment} | {image}"
        )
    return "\n".join(lines)


def select_image_rows(session: Session) -> list[Rated]:
    """The rated items whose thumbnails are offered: at most 6 loved, 6 liked and 4 disliked,
    loved first, newest first within a rating, only those with an https thumbnail."""
    rated = rated_map(session)
    picked: list[Rated] = []
    counts = {"love": 0, "like": 0, "dislike": 0}
    for row in rated_results(session):  # newest rating first
        r = rated[row["result"].id]
        if r.thumbnail_url.startswith("https://") and counts[r.rating] < MAX_IMAGES[r.rating]:
            counts[r.rating] += 1
            picked.append(r)
    order = {"love": 0, "like": 1, "dislike": 2}
    picked.sort(key=lambda r: order[r.rating])
    return picked


def fetch_images(
    rows: list[Rated], thumbnailer: Thumbnailer | None, config_budget: Budget
) -> tuple[dict[str, PreparedImage], ImageReport]:
    """Fetch the offered thumbnails with the guarded fetcher (https, public hosts, size and time
    limits). A failure leaves that item to go by title; reasons are recorded, never urls."""
    report = ImageReport(offered=len(rows))
    if thumbnailer is None or not rows:
        return {}, report
    config = config_budget.config

    def one(row: Rated) -> tuple[str, PreparedImage | None, str]:
        try:
            got = fetch_traced(thumbnailer, row.thumbnail_url, config)
        except Exception as exc:  # a fetcher bug must not stop Shop Again
            return row.id, None, type(exc).__name__
        return row.id, got.image if got.ok else None, f"{got.host}: {got.reason}"

    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS, thread_name_prefix="prefimg") as pool:
        results = list(pool.map(one, rows))
    images: dict[str, PreparedImage] = {}
    for rid, image, reason in results:
        if image is not None:
            images[rid] = image
        else:
            report.reasons.append(reason[:80])
    report.used = len(images)
    report.failed = report.offered - report.used
    report.reasons = report.reasons[:8]
    return images, report


def build_profile(
    session: Session,
    base: ShopQuery,
    budget: Budget,
    thumbnailer: Thumbnailer | None = None,
    fetcher: Callable[..., tuple[dict[str, PreparedImage], ImageReport]] = fetch_images,
) -> tuple[StoredProfile, str]:
    """Update the session's profile from its ratings (prompt v2). Returns (profile, warning).

    Offered thumbnails are attached when they can be fetched; the rest go by title and the report
    says so. An unusable model answer keeps the existing profile and says so. A budget error
    propagates.
    """
    config = budget.config
    rows = select_image_rows(session)
    images, report = fetcher(rows, thumbnailer, budget)
    ordered = [r.id for r in rows if r.id in images]
    numbers = {rid: n for n, rid in enumerate(ordered, start=1)}
    try:
        completion = complete(
            span_name="llm.shop_prefs",
            prompt=load_prompt("shop_prefs"),
            model=config.model_retrieval,
            user_text=prefs_user_text(session, base, numbers),
            budget=budget,
            max_tokens=PREFS_MAX_TOKENS,
            schema=to_output_schema(PreferenceProfileV2),
            images=[images[rid] for rid in ordered] or None,
            effort=config.effort_retrieval,
        )
        proposed = parse_model(PreferenceProfileV2, completion.text, what="shop_prefs")
    except (LLMOutputError, SchemaError) as exc:
        kept = merge_profile_v2(session.profile, None, session, base, report, set(images))
        return kept, f"Could not update your preferences ({exc}); used the earlier ones."
    return merge_profile_v2(session.profile, proposed, session, base, report, set(images)), ""
