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

import hashlib
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.images import PreparedImage
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import SchemaError, parse_model, to_output_schema
from app.shop.decide import (
    DIMENSION_LABELS,
    FALLBACK_NOTE,
    Exposure,
    TagMap,
    decide,
    typed_terms_of,
)
from app.shop.models import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_TERMS,
    ContradictedGuess,
    ImageReport,
    InferredAttribute,
    ItemTags,
    PreferenceProfile,
    PreferenceProfileV2,
    PreferenceTags,
    ProfileEntry,
    QueryAdjustments,
    ShopQuery,
    ShopResult,
    StoredProfile,
)
from app.shop.parse import strip_price_words
from app.shop.retailers import seller_key, tier_of
from app.shop.retailers import seller_key as decide_seller_key
from app.shop.store import Session, now_iso, rated_results
from app.shop.textutil import (
    Rated,
    mentions,
    normalize_phrase,
    phrases_match,
    sig_words,
    text_matches,
)
from app.shop.vocab import (
    PHRASE_DIMS,
    normalize,
    normalize_style,
    phrase_word,
    style_families,
    style_leaves,
    tag_values,
    title_value,
    vocab_text,
)
from app.thumbnails import Thumbnailer, fetch_traced

MAX_PROFILE_TERMS = 12
MAX_PRICE_CUT = 0.20  # price_max may fall by at most this fraction of the typed cap. A guess.
MAX_RATED_IN_PROMPT = 60
PREFS_MAX_TOKENS = (
    3000  # the least the preference call may be given (stage 2c had 2000 and ran out)
)
MAX_PREFS_TOKENS = 12000
ESTIMATED_BASE_TOKENS = 450  # stated likes and dislikes, guesses, adjustments
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


def _guess_state(
    previous: StoredProfile,
    base: ShopQuery,
    contradicted_by_model: list[Any],
    rated: dict[str, Rated],
    removed: list[str],
) -> tuple[list[str], list[str]]:
    """(guesses still shown, guesses dropped so far): a parser style term the typed request does not
    contain is a guess; it is dropped when the model reports a loved or liked item that contradicts
    it and no loved or liked title shows it."""
    positive_titles = [r.title for r in rated.values() if r.rating in ("love", "like")]
    contradicted: list[str] = []
    for c in contradicted_by_model:
        key = _short(c.text)
        ids = [i for i in c.evidence if i in rated and rated[i].rating in ("love", "like")]
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
    return guesses, dropped


def merge_profile_v3(
    existing: StoredProfile | None,
    proposed: PreferenceTags | None,
    session: Session,
    base: ShopQuery,
    images: ImageReport,
    image_ids: set[str],
    tagging: dict[str, Any] | None = None,
    exposure: Exposure | None = None,
) -> StoredProfile:
    """The stored profile after one v3 preference call: the model's per-item tags go to
    `app.shop.decide`, which decides every inferred entry from ALL the session's ratings. The
    shopper's own words (stated likes and dislikes, a price ask from a comment) come from the model
    as before and keep full weight. If the call failed (`proposed=None`, or it tagged no item) the
    profile is decided in fallback mode: no attribute at all (titles alone are biased), only seller
    affinity and price, and the stated part is kept as it was."""
    previous = existing or StoredProfile()
    rated_count = sum(1 for rec in session.ratings.values() if rec.rating is not None)
    rated = rated_map(session)
    if proposed is None:
        stated = previous
        tags: dict[str, ItemTags] = {}
        model_guesses: list[Any] = []
    else:
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
        tags = {t.id: t for t in proposed.items if t.id in rated}
        model_guesses = list(proposed.contradicted_guesses)
    removed = stated.removed
    fallback = proposed is None or (not tags and bool(rated))
    decisions = decide(
        rated,
        tags,
        list(session.results.values()),
        removed,
        typed_terms_of(base),
        image_ids,
        fallback=fallback,
        exposure=exposure,
    )
    info = dict(tagging or {})
    info.setdefault("items_total", len(rated))
    info["items_tagged"] = len(tags)
    info["fallback"] = fallback
    info["mode"] = "fallback" if fallback else "model"
    pinned = list(previous.pinned_sellers)
    entries = [
        e.model_copy(update={"pinned": pin_key(e) in pinned})
        if e.attr_kind in ("seller", "tier")
        else e
        for e in decisions.entries
        if not (e.polarity == "like" and e.text in stated.likes and e.status == "converged")
        and not (e.polarity == "dislike" and e.text in stated.dislikes)
    ]  # a phrase the shopper said is shown once, as theirs
    guesses, dropped = _guess_state(previous, base, model_guesses, rated, removed)
    return stated.model_copy(
        update={
            "inferred": entries,
            "guesses": guesses,
            "dropped_guesses": dropped,
            "images": images,
            "pinned_sellers": pinned,
            "rated_count": rated_count,
            "trace": decisions.trace,
            "tagging": info,
        }
    )


def pin_key(entry: ProfileEntry) -> str:
    """What a pin stores for a seller chip (its seller key) or a tier chip ("tier:<tier>")."""
    if entry.attr_kind == "tier":
        return f"tier:{entry.values[0]}" if entry.values else "tier:?"
    return decide_seller_key(seller_name(entry))


def with_pins(profile: StoredProfile, pinned: list[str]) -> StoredProfile:
    """The profile with `pinned` (seller keys) as its pins and the seller chip flagged to match."""
    entries = [
        e.model_copy(update={"pinned": pin_key(e) in pinned})
        if e.attr_kind in ("seller", "tier")
        else e
        for e in profile.inferred
    ]
    return profile.model_copy(update={"pinned_sellers": list(pinned), "inferred": entries})


def pin_seller(profile: StoredProfile, phrase: str) -> StoredProfile | None:
    """Toggle the pin on the seller or tier chip named by `phrase` (None: no such chip). A pinned
    seller may take up to `SELLER_PINNED_SHARE` of a batch instead of `SELLER_MAX_SHARE`; a pinned
    tier up to 80% instead of `TIER_BATCH_SHARE`."""
    key = _short(phrase)
    chip = next(
        (
            e
            for e in profile.inferred
            if e.attr_kind in ("seller", "tier") and _short(e.text) == key
        ),
        None,
    )
    if chip is None:
        return None
    seller = pin_key(chip)
    pinned = (
        [k for k in profile.pinned_sellers if k != seller]
        if seller in profile.pinned_sellers
        else [*profile.pinned_sellers, seller]
    )
    return with_pins(profile, pinned)


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


def converged_entries(profile: StoredProfile) -> list[ProfileEntry]:
    return [e for e in profile.inferred if e.polarity == "like" and e.status == "converged"]


def open_dimension(profile: StoredProfile) -> ProfileEntry | None:
    """The one open dimension that search varies: among the open ones that have search words, the
    one with the most like mass (a tie goes to the earlier dimension: shape first)."""
    opens = [
        e
        for e in profile.inferred
        if e.status == "open" and e.dimension in PHRASE_DIMS and len(e.values) >= 2
    ]
    opens.sort(key=lambda e: (-e.weight, PHRASE_DIMS.index(e.dimension)))
    return opens[0] if opens else None


def build_phrasings(
    base: ShopQuery, profile: StoredProfile, adjusted: ShopQuery, max_calls: int = MAX_QUERIES
) -> tuple[list[str], list[str]]:
    """The search phrasings for a Shop Again and the ones dropped to fit `max_calls` (one Serper
    call each, at least). Pressure comes only from what converged; an open dimension exerts none:

      * the typed query as adjusted by the shopper's comments (never carries a learned value or the
        seller's name, so look-alikes from anywhere appear);
      * one phrasing per value of the open dimension (shape: round, rectangular), each with the
        converged material, finish and legs, never the same open value in every phrasing;
      * the seller's name plus the product, when a seller affinity converged;
      * the product plus the strongest converged material, finish and shape words.

    Priority when the cap bites: typed, seller, the open values (kept or dropped together, so one
    value is never favoured over another; each carries the converged words), and last the converged
    words alone (only run when there are no open values, or when the open values were dropped).
    Output order is: typed, converged words, seller, open values. Duplicates after normalizing are
    dropped.
    """
    product = _product_noun(base)
    converged = [
        e
        for e in converged_entries(profile)
        if e.dimension in PHRASE_DIMS and e.status == "converged"
    ]
    converged.sort(key=lambda e: (-e.weight, e.text))
    open_entry = open_dimension(profile)
    seller = next((e for e in profile.inferred if e.attr_kind == "seller"), None)

    def clean(text: str) -> str:
        return " ".join(strip_price_words(text).split())[:MAX_SEARCH_TEXT_CHARS]

    typed = clean(adjusted.search_text)
    attrs = [e for e in converged if open_entry is None or e.dimension != open_entry.dimension]
    attr_words = " ".join(phrase_word(e.dimension, e.values[0]) for e in attrs[:3])
    attr_phrase = clean(f"{product} {attr_words}") if attrs and product else ""
    seller_phrase = (
        clean(
            f"{seller_name(seller)} {product} {adjusted.must_have[0] if adjusted.must_have else ''}"
        )
        if seller is not None and product
        else ""
    )
    open_phrases: list[str] = []
    if open_entry is not None and product:
        # each open value carries the converged words, so the pressure rides along
        extras = " ".join(phrase_word(e.dimension, e.values[0]) for e in attrs[:3])
        for value in open_entry.values[:3]:
            open_phrases.append(
                clean(f"{product} {phrase_word(open_entry.dimension, value)} {extras}")
            )

    # (priority, display position, texts); a lower priority number survives longer. The open values
    # carry the converged words, so the standalone converged phrasing only runs when they do not.
    slots: list[tuple[int, int, list[str]]] = [
        (0, 0, [typed]),
        (1, 2, [seller_phrase] if seller_phrase else []),
        (2, 3, open_phrases),
        (3, 1, [attr_phrase] if attr_phrase and not open_phrases else []),
    ]
    chosen: list[tuple[int, list[str]]] = []
    dropped: list[str] = []
    used = 0
    open_dropped = False
    seen: set[str] = set()
    for _, position, texts in sorted(slots):
        fresh = [t for t in texts if t and normalize_phrase(t) not in seen]
        if not fresh:
            continue
        if used + len(fresh) > max(1, max_calls):  # a group is kept whole or dropped whole
            dropped.extend(fresh)
            if texts is open_phrases:
                open_dropped = True
            continue
        used += len(fresh)
        seen.update(normalize_phrase(t) for t in fresh)
        chosen.append((position, fresh))
    if open_dropped and attr_phrase and used + 1 <= max(1, max_calls):
        chosen.append((1, [attr_phrase]))  # the open values did not fit: the converged words alone
    queries = [t for _, texts in sorted(chosen) for t in texts]
    return queries, dropped


def build_queries(base: ShopQuery, profile: StoredProfile, adjusted: ShopQuery) -> list[str]:
    """`build_phrasings` without the cap on Serper calls."""
    return build_phrasings(base, profile, adjusted, max_calls=10)[0]


# ---- the summary line and the alignment measure ----


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def profile_summary(profile: StoredProfile | None) -> str:
    """A plain sentence for the page, from the profile alone (no model call):

    "Looking for more brushed brass and wood and metal pieces from West Elm in the upper price
    range, fewer too dark. Shape is open: showing both round and rectangular. Price is mixed."

    Stated and converged inferred likes are joined; a seller affinity and a price band add "from
    <seller>" and "in the <upper|lower> price range". An open dimension says it is open and which
    values are shown; mixed price says so. Empty when there is nothing.
    """
    if profile is None:
        return ""
    inferred_likes = [
        e
        for e in profile.inferred
        if e.polarity == "like"
        and e.status == "converged"
        and e.attr_kind not in ("seller", "tier", "price_band", "price_mixed")
    ]
    likes = list(dict.fromkeys([*profile.likes, *[e.text for e in inferred_likes]]))[:4]
    dislikes = list(
        dict.fromkeys(
            [*profile.dislikes, *[e.text for e in profile.inferred if e.polarity == "dislike"]]
        )
    )[:4]
    seller = next((e for e in profile.inferred if e.attr_kind == "seller"), None)
    tier = next((e for e in profile.inferred if e.attr_kind == "tier"), None)
    band = next((e for e in profile.inferred if e.attr_kind == "price_band"), None)
    parts: list[str] = []
    if likes or seller or tier or band:
        text = f"more {_join(likes)}" if likes else "more pieces"
        if likes and (seller or tier or band):
            text += " pieces"
        if seller and tier:
            text += f" from {seller_name(seller)} and similar retailers"
        elif seller:
            text += f" from {seller_name(seller)}"
        elif tier:
            text += f" from retailers like {tier.text.removeprefix('retailers like ')}"
        if band:
            text += f" in the {band.band} price range"
        parts.append(text)
    if dislikes:
        parts.append(f"fewer {_join(dislikes)}")
    text = f"Looking for {', '.join(parts)}." if parts else ""
    for e in profile.inferred:
        if e.status == "open" and e.values:
            label = DIMENSION_LABELS.get(e.dimension, e.dimension.capitalize())
            text += f" {label} is open: showing both {_join(e.values)}."
    if any(e.status == "mixed" for e in profile.inferred):
        text += " Price is mixed."
    if profile.adjustments.price_note:
        text += f" {profile.adjustments.price_note}"
    return text.strip()


def alignment_signals(profile: StoredProfile | None) -> int:
    """How many decided signals a result can be matched against: converged inferred likes (an
    attribute, a seller, a retailer tier), a price band (a mixed price is no signal) and the
    shopper's own stated likes. Zero means nothing has been decided yet."""
    if profile is None:
        return 0
    converged = [e for e in profile.inferred if e.polarity == "like" and e.status == "converged"]
    return len(converged) + len(profile.likes)


def alignment_counts(
    results: list[ShopResult], profile: StoredProfile | None, tags: TagMap | None = None
) -> tuple[int, int]:
    """(results that match, results): a match is any decided like and no dislike.

    A result matches a decided like when its tags (the model's, when `tags` has the result; else
    the title's keywords) show the value for a dimension such as shape or material (a style by its
    family), or the words of the phrase otherwise; it comes from the affinity seller or a retailer
    in the affinity tier; its price is inside a price band; or its title shows one of the
    shopper's own stated likes. It matches a dislike when its title shows an inferred or stated
    dislike. Guess, open and mixed chips do not count. With no decided signal (see
    `alignment_signals`) nothing matches; callers show "n/a" then, not 0.
    """
    if not results or profile is None:
        return 0, len(results)
    likes = [e for e in profile.inferred if e.polarity == "like" and e.status == "converged"]
    if not likes and not profile.likes:
        return 0, len(results)
    dislike_entries = [e for e in profile.inferred if e.polarity == "dislike"]

    def shows(e: ProfileEntry, r: ShopResult) -> bool:
        if e.dimension and e.values and e.attr_kind not in ("seller", "price_band", "tier"):
            tagged = (tags or {}).get(r.id)
            if tagged is not None:
                got = tagged.get(e.dimension)
            else:
                got = title_value(e.dimension, r.title)
            if got is not None or e.dimension in (
                "shape",
                "material",
                "color_finish",
                "frame_color",
            ):
                return e.values[0] in tag_values(e.dimension, got)
        return text_matches(e.text, r.title)

    def matches_like(r: ShopResult) -> bool:
        if any(text_matches(t, r.title) for t in profile.likes):
            return True
        for e in likes:
            if e.attr_kind == "seller":
                if seller_key(r.seller) == seller_key(seller_name(e)):
                    return True
            elif e.attr_kind == "tier":
                if e.values and tier_of(r.seller) == e.values[0]:
                    return True
            elif e.attr_kind == "price_band":
                if r.price is not None and e.low is not None and e.high is not None:
                    if e.low <= r.price <= e.high:
                        return True
            elif shows(e, r):
                return True
        return False

    def matches_dislike(r: ShopResult) -> bool:
        return any(shows(e, r) for e in dislike_entries) or any(
            text_matches(d, r.title) for d in profile.dislikes
        )

    hits = sum(1 for r in results if matches_like(r) and not matches_dislike(r))
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
    session: Session,
    base: ShopQuery,
    image_numbers: dict[str, int] | None = None,
    only_ids: set[str] | None = None,
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
        "Allowed values (shape, material, color_finish, frame_color, style; use these or null):",
        vocab_text(),
        "",
        "Rated results (loved first; image N is the Nth attached image, none means title only):",
    ]
    rows = rated_results(session)
    order = {"love": 0, "like": 1, "dislike": 2}
    rows.sort(key=lambda r: order[r["rating"]])  # stable: newest first within a rating
    if only_ids is not None:
        rows = [row for row in rows if row["result"].id in only_ids]
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


def estimate_item_tokens() -> int:
    """Output tokens one tagged item takes, from the schema: a worst-case item (every field filled
    with a long value, three cues, a long id) as compact JSON, at about 3 characters a token."""
    sample = ItemTags(
        id="5100000000001004736", shape="rectangular", material="stone",
        color_finish="warm wood", frame_color="brass or gold", style=["mid-century", "industrial"],
        quality_cues=["solid wood construction", "brushed brass finish", "natural stone top"],
    )  # fmt: skip
    return int(len(sample.model_dump_json()) / 3) + 1


def prefs_max_tokens(n_items: int) -> int:
    """`max_tokens` for the preference call: twice the estimated output (items plus the stated
    likes, dislikes, guesses and adjustments), never under `PREFS_MAX_TOKENS`, never over 12,000."""
    needed = ESTIMATED_BASE_TOKENS + n_items * estimate_item_tokens()
    return min(MAX_PREFS_TOKENS, max(PREFS_MAX_TOKENS, 2 * needed))


def split_groups(session: Session) -> list[set[str]]:
    """How the retry splits the rated items: loved and liked first, then disliked. With no disliked
    item, loved then liked. Empty groups are left out."""
    rows = rated_results(session)[:MAX_RATED_IN_PROMPT]
    positive = {r["result"].id for r in rows if r["rating"] in ("love", "like")}
    disliked = {r["result"].id for r in rows if r["rating"] == "dislike"}
    if disliked:
        groups = [positive, disliked]
    else:
        groups = [
            {r["result"].id for r in rows if r["rating"] == "love"},
            {r["result"].id for r in rows if r["rating"] == "like"},
        ]
    return [g for g in groups if g]


def merge_tag_answers(answers: list[PreferenceTags]) -> PreferenceTags:
    """One answer from the split calls: items joined, stated lists united, the first answer's
    adjustments with the lowest price cap any answer asked for."""
    items: dict[str, ItemTags] = {}
    likes: list[str] = []
    dislikes: list[str] = []
    guesses: dict[str, Any] = {}
    for a in answers:
        for t in a.items:
            items.setdefault(t.id, t)
        likes += [x for x in a.likes if x not in likes]
        dislikes += [x for x in a.dislikes if x not in dislikes]
        for c in a.contradicted_guesses:
            guesses.setdefault(c.text, c)
    adjustments = answers[0].query_adjustments
    caps = [a.query_adjustments.price_max for a in answers if a.query_adjustments.price_max]
    notes = [a.query_adjustments.price_note for a in answers if a.query_adjustments.price_note]
    adjustments = adjustments.model_copy(
        update={"price_max": min(caps) if caps else None, "price_note": notes[0] if notes else ""}
    )
    return PreferenceTags(
        items=list(items.values()), likes=likes, dislikes=dislikes,
        contradicted_guesses=list(guesses.values()), query_adjustments=adjustments,
    )  # fmt: skip


# ---- stage 2h: readings prepared in the background (photo tags, comments) ------------------


def comment_key(rid: str, comment: str) -> str:
    """The cache key of one comment's reading: the item id plus the comment text, so a changed or
    deleted comment never finds an old entry."""
    digest = hashlib.sha1(" ".join(comment.split()).lower().encode()).hexdigest()[:12]
    return f"{rid}|{digest}"


def reading_entry(answer: PreferenceTags, rid: str) -> dict[str, Any]:
    """What is cached for one rated item from a preference call restricted to it."""
    item = next((t for t in answer.items if t.id == rid), None)
    tags: dict[str, str | None] = {}
    if item is not None:
        for dim in ("shape", "material", "color_finish", "frame_color"):
            tags[dim] = normalize(dim, getattr(item, dim))
        tags["style"] = normalize_style(item.style)
    return {
        "tags": tags,
        "cues": list(item.quality_cues) if item is not None else [],
        "likes": list(answer.likes),
        "dislikes": list(answer.dislikes),
        "adjustments": answer.query_adjustments.model_dump(mode="json"),
    }


def merge_adjustments(parts: list[QueryAdjustments]) -> QueryAdjustments:
    """Several items' query adjustments as one: lists united, the lowest price cap asked for."""

    def united(name: str) -> list[str]:
        out: list[str] = []
        for part in parts:
            out += [x for x in getattr(part, name) if x not in out]
        return out

    caps = [p.price_max for p in parts if p.price_max]
    notes = [p.price_note for p in parts if p.price_note]
    searches = [p.search_text for p in parts if p.search_text]
    return QueryAdjustments(
        search_text=searches[0] if searches else "",
        style_terms=united("style_terms"), material_terms=united("material_terms"),
        must_have=united("must_have"), avoid=united("avoid"),
        price_max=min(caps) if caps else None, price_note=notes[0] if notes else "",
    )  # fmt: skip


def contradicted_from_tags(
    base: ShopQuery, tags: dict[str, ItemTags], ratings: dict[str, str]
) -> list[ContradictedGuess]:
    """The parser's style guesses that loved or liked items clearly are not, from their tags: a
    guess is contradicted by an item whose style families are known and do not include the
    guess's family. The model used to say this; with cached tags code can."""
    out: list[ContradictedGuess] = []
    for term in guess_style_terms(base):
        leaf = normalize("style", term)
        families = style_families(leaf) if leaf and leaf != "other" else []
        if not families:
            continue
        ids = [
            rid
            for rid, t in tags.items()
            if ratings.get(rid) in ("love", "like")
            and style_families(normalize_style(t.style))
            and families[0] not in style_families(normalize_style(t.style))
        ]
        if ids:
            out.append(ContradictedGuess(text=term, evidence=sorted(ids)))
    return out


def cached_readings(
    session: Session, base: ShopQuery
) -> tuple[PreferenceTags | None, set[str], set[str]]:
    """The rated items the background preparation already read: (their reading as one answer, the
    rated item ids still missing, the ids whose tags came from a photo).

    An item is covered when it has photo tags (or a comment reading that carries tags) and, if it
    has a comment, a reading of that exact comment. Nothing else is trusted: an item without both
    is missing and goes to the live preference call."""
    items: list[ItemTags] = []
    likes: list[str] = []
    dislikes: list[str] = []
    adjustments: list[QueryAdjustments] = []
    missing: set[str] = set()
    photo_ids: set[str] = set()
    ratings: dict[str, str] = {}
    for rid, rec in session.ratings.items():
        if rec.rating is None or rid not in session.results:
            continue
        comment = rec.comment.strip()
        reading = session.comment_cache.get(comment_key(rid, comment)) if comment else None
        tags: dict[str, str | None] | None = None
        if session.tag_source.get(rid) == "photo" and rid in session.tag_cache:
            tags = dict(session.tag_cache[rid])
            photo_ids.add(rid)
        elif reading is not None and any(reading["tags"].values()):
            tags = dict(reading["tags"])
            photo_ids.add(rid)
        if tags is None or (comment and reading is None):
            missing.add(rid)
            continue
        ratings[rid] = rec.rating
        cues = session.quality_cues.get(rid) or (reading["cues"] if reading else [])
        items.append(
            ItemTags(
                id=rid,
                shape=tags.get("shape"),
                material=tags.get("material"),
                color_finish=tags.get("color_finish"),
                frame_color=tags.get("frame_color"),
                style=style_leaves(tags.get("style")),
                quality_cues=list(cues)[:3],
            )  # fmt: skip
        )
        if reading is not None:
            likes += [x for x in reading["likes"] if x not in likes]
            dislikes += [x for x in reading["dislikes"] if x not in dislikes]
            adjustments.append(QueryAdjustments.model_validate(reading["adjustments"]))
    if not items:
        return None, missing, photo_ids
    merged = merge_adjustments(adjustments) if adjustments else QueryAdjustments(
        search_text="", style_terms=[], material_terms=[], must_have=[], avoid=[],
        price_max=None, price_note="",
    )  # fmt: skip
    answer = PreferenceTags(
        items=items, likes=likes, dislikes=dislikes,
        contradicted_guesses=contradicted_from_tags(base, {t.id: t for t in items}, ratings),
        query_adjustments=merged,
    )  # fmt: skip
    return answer, missing, photo_ids


def read_rated_items(
    session: Session,
    base: ShopQuery,
    only: set[str],
    budget: Budget,
    thumbnailer: Thumbnailer | None,
) -> tuple[PreferenceTags | None, ImageReport, str]:
    """One preference call (prompt shop_prefs v6) for just the rated items `only`, each with its
    thumbnail when it can be fetched: (the answer or None, the image report, why it failed). Used
    by the background preparation to read a comment as it is written."""
    config = budget.config
    rated = rated_map(session)
    rows = [
        rated[rid]
        for rid in only
        if rid in rated and rated[rid].thumbnail_url.startswith("https://")
    ]
    images, report = fetch_images(rows, thumbnailer, budget)
    ordered = [r.id for r in rows if r.id in images]
    numbers = {rid: n for n, rid in enumerate(ordered, start=1)}
    try:
        completion = complete(
            span_name="llm.shop_prefs",
            prompt=load_prompt("shop_prefs"),
            model=config.model_retrieval,
            user_text=prefs_user_text(session, base, numbers, only),
            budget=budget,
            max_tokens=prefs_max_tokens(len(only)),
            schema=to_output_schema(PreferenceTags),
            images=[images[rid] for rid in ordered] or None,
            effort=config.effort_retrieval,
            disable_thinking=True,
        )
        return parse_model(PreferenceTags, completion.text, what="shop_prefs"), report, ""
    except (LLMOutputError, SchemaError) as exc:
        return None, report, str(exc)[:120]


def build_profile(
    session: Session,
    base: ShopQuery,
    budget: Budget,
    thumbnailer: Thumbnailer | None = None,
    fetcher: Callable[..., tuple[dict[str, PreparedImage], ImageReport]] = fetch_images,
    exposure: Exposure | Callable[[], Exposure] | None = None,
) -> tuple[StoredProfile, str]:
    """Update the session's profile from its ratings (prompt v4). Returns (profile, warning).

    The model tags each rated item (with its thumbnail when it can be fetched) and reads the
    comments; `merge_profile_v3` decides the preferences in code. The call gets a token budget
    computed from the schema (twice the estimate). If it ends with max_tokens or invalid JSON it is
    retried ONCE as split calls (loved and liked, then disliked); a failed split leaves its items
    untagged; if nothing could be tagged the profile is decided in fallback mode (seller and price
    only) and the warning says so. A budget error propagates. The profile records, in `tagging`,
    items tagged of rated, images used, and whether a retry or the fallback happened.
    """
    config = budget.config
    n_rated = sum(1 for rec in session.ratings.values() if rec.rating is not None)
    cached, missing, photo_ids = cached_readings(session, base)
    need: set[str] | None = None  # None: every rated item goes to the call (nothing is cached)
    if cached is not None:
        need = missing
    rows = [r for r in select_image_rows(session) if need is None or r.id in need]
    images, report = fetcher(rows, thumbnailer, budget) if rows else ({}, ImageReport())
    calls = 0
    failures: list[str] = []

    def call(only: set[str] | None) -> PreferenceTags:
        nonlocal calls
        subset = [r for r in rows if r.id in images and (only is None or r.id in only)]
        ordered = [r.id for r in subset]
        numbers = {rid: n for n, rid in enumerate(ordered, start=1)}
        count = n_rated if only is None else len(only)
        calls += 1
        completion = complete(
            span_name="llm.shop_prefs",
            prompt=load_prompt("shop_prefs"),
            model=config.model_retrieval,
            user_text=prefs_user_text(session, base, numbers, only),
            budget=budget,
            max_tokens=prefs_max_tokens(count),
            schema=to_output_schema(PreferenceTags),
            images=[images[rid] for rid in ordered] or None,
            effort=config.effort_retrieval,
            disable_thinking=True,
        )
        return parse_model(PreferenceTags, completion.text, what="shop_prefs")

    proposed: PreferenceTags | None = None
    retried = False
    if need is not None and not need:
        proposed = cached  # everything was read in the background: no model call at all
    else:
        try:
            proposed = call(need)
        except (LLMOutputError, SchemaError) as exc:
            failures.append(str(exc)[:120])
            retried = True
            answers: list[PreferenceTags] = []
            for group in split_groups(session):
                if need is not None:
                    group = group & need
                if not group:
                    continue
                try:
                    answers.append(call(group))
                except (LLMOutputError, SchemaError) as again:
                    failures.append(str(again)[:120])
            proposed = merge_tag_answers(answers) if answers else None
        if cached is not None:  # the cached part and the live part are one answer
            proposed = merge_tag_answers([cached, proposed]) if proposed is not None else cached
    tagging: dict[str, Any] = {
        "from_cache": len(cached.items) if cached is not None else 0,
        "model_items": n_rated - (len(cached.items) if cached is not None else 0),
        "items_total": n_rated,
        "images_offered": report.offered,
        "images_used": report.used,
        "retried": retried,
        "model_calls": calls,
        "failures": failures,
    }
    profile = merge_profile_v3(
        session.profile,
        proposed,
        session,
        base,
        report,
        set(images) | photo_ids,
        tagging,
        exposure() if callable(exposure) else exposure,
    )
    warning = ""
    if profile.tagging.get("fallback"):
        warning = FALLBACK_NOTE
    elif profile.tagging["items_tagged"] < n_rated:
        warning = f"Read details for {profile.tagging['items_tagged']} of {n_rated} rated items."
    return profile, warning
