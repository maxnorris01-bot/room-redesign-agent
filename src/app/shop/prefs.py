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
"""

from __future__ import annotations

from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import SchemaError, parse_model, to_output_schema
from app.shop.models import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_TERM_CHARS,
    MAX_TERMS,
    PreferenceProfile,
    QueryAdjustments,
    ShopQuery,
    ShopResult,
    StoredProfile,
)
from app.shop.parse import strip_price_words
from app.shop.store import Session, now_iso, rated_results

MAX_PROFILE_TERMS = 12
MAX_PRICE_CUT = 0.20  # price_max may fall by at most this fraction of the typed cap. A guess.
MAX_RATED_IN_PROMPT = 60
PREFS_MAX_TOKENS = 1200
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


def remove_entry(profile: StoredProfile, kind: str, phrase: str) -> StoredProfile | None:
    """The profile without one chip, its phrase added to the deleted list; None if the chip is not
    there. Adjustment terms that match the phrase go too, so the next search drops them."""
    key = normalize_phrase(phrase)
    entries = profile.likes if kind == "like" else profile.dislikes
    if key not in entries:
        return None
    removed = [*profile.removed, key] if key not in profile.removed else list(profile.removed)
    adj = profile.adjustments

    def drop(values: list[str]) -> list[str]:
        return [v for v in values if not phrases_match(v, key)]

    return profile.model_copy(
        update={
            "likes": [x for x in profile.likes if x != key] if kind == "like" else profile.likes,
            "dislikes": (
                [x for x in profile.dislikes if x != key] if kind == "dislike" else profile.dislikes
            ),
            "removed": removed[-50:],
            "adjustments": adj.model_copy(
                update={
                    "style_terms": drop(adj.style_terms),
                    "material_terms": drop(adj.material_terms),
                    "must_have": drop(adj.must_have),
                    "avoid": drop(adj.avoid),
                    "search_text": "" if phrases_match(adj.search_text, key) else adj.search_text,
                }
            ),
        }
    )


def apply_adjustments(base: ShopQuery, profile: StoredProfile | None) -> ShopQuery:
    """The query for a Shop Again: the typed query plus the profile's adjustments.

    Category, category key and the price floor are the typed query's; `price_max` is lowered only
    by an accepted adjustment. Terms are added after the typed ones, without repeats.
    """
    if profile is None:
        return base
    adj = profile.adjustments

    def union(first: list[str], extra: list[str]) -> list[str]:
        return [*first, *[t for t in extra if t not in first]][:MAX_TERMS]

    price_max = base.price_max
    if adj.price_max is not None and (price_max is None or adj.price_max < price_max):
        price_max = clamp_price_max(base.price_max, adj.price_max) or price_max
    return base.model_copy(
        update={
            "search_text": adj.search_text or base.search_text,
            "style_terms": union(base.style_terms, adj.style_terms),
            "material_terms": union(base.material_terms, adj.material_terms),
            "must_have": union(base.must_have, adj.must_have),
            "avoid": union(base.avoid, adj.avoid),
            "price_max": price_max,
        }
    )


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def profile_summary(profile: StoredProfile | None) -> str:
    """A plain sentence for the page, from the profile alone: "Looking for more boucle fabric and
    low profile, fewer too dark." Empty when there is nothing to say."""
    if profile is None:
        return ""
    parts: list[str] = []
    if profile.likes:
        parts.append(f"more {_join(profile.likes[:4])}")
    if profile.dislikes:
        parts.append(f"fewer {_join(profile.dislikes[:4])}")
    text = f"Looking for {', '.join(parts)}." if parts else ""
    if profile.adjustments.price_note:
        text = f"{text} {profile.adjustments.price_note}".strip()
    return text


# ---- the model call ------------------------------------------------------------------------------


def _money(value: float | None) -> str:
    return "no price" if value is None else f"${value:,.0f}"


def _flat(text: str, limit: int) -> str:
    return " ".join(text.replace("|", "/").split())[:limit]


def prefs_user_text(session: Session, base: ShopQuery) -> str:
    profile = session.profile or StoredProfile()
    adj = profile.adjustments
    price = "none" if base.price_max is None else f"up to {_money(base.price_max)}"
    lines = [
        f"Original request: {base.request}",
        f"Parsed query: search_text={base.search_text}; category={base.category or 'unknown'}; "
        f"price={price}; style={', '.join(base.style_terms) or 'none'}; "
        f"materials={', '.join(base.material_terms) or 'none'}; "
        f"must_have={', '.join(base.must_have) or 'none'}; avoid={', '.join(base.avoid) or 'none'}",
        "Existing profile:",
        f"  likes: {'; '.join(profile.likes) or 'none'}",
        f"  dislikes: {'; '.join(profile.dislikes) or 'none'}",
        f"  adjustments: search_text={adj.search_text or 'none'}; "
        f"style={', '.join(adj.style_terms) or 'none'}; "
        f"materials={', '.join(adj.material_terms) or 'none'}; "
        f"must_have={', '.join(adj.must_have) or 'none'}; avoid={', '.join(adj.avoid) or 'none'}",
        f"Removed entries (never bring these back): {'; '.join(profile.removed) or 'none'}",
        "",
        "Rated results (loved first):",
    ]
    rows = rated_results(session)
    order = {"love": 0, "like": 1, "dislike": 2}
    rows.sort(key=lambda r: order[r["rating"]])  # stable: newest first within a rating
    for row in rows[:MAX_RATED_IN_PROMPT]:
        r: ShopResult = row["result"]
        comment = _flat(row["comment"], 200) or "none"
        lines.append(
            f"- {row['rating']} | {_flat(r.title, 100)} | {_money(r.price)} | "
            f"{_flat(r.seller, 40) or '?'} | comment: {comment}"
        )
    return "\n".join(lines)


def build_profile(session: Session, base: ShopQuery, budget: Budget) -> tuple[StoredProfile, str]:
    """Update the session's profile from its ratings. Returns (profile, warning); an unusable model
    answer keeps the existing profile and says so. A budget error propagates."""
    config = budget.config
    rated = sum(1 for rec in session.ratings.values() if rec.rating is not None)
    try:
        completion = complete(
            span_name="llm.shop_prefs",
            prompt=load_prompt("shop_prefs"),
            model=config.model_retrieval,
            user_text=prefs_user_text(session, base),
            budget=budget,
            max_tokens=PREFS_MAX_TOKENS,
            schema=to_output_schema(PreferenceProfile),
            effort=config.effort_retrieval,
        )
        proposed = parse_model(PreferenceProfile, completion.text, what="shop_prefs")
    except (LLMOutputError, SchemaError) as exc:
        kept = merge_profile(session.profile, None, base, rated)
        return kept, f"Could not update your preferences ({exc}); used the earlier ones."
    return merge_profile(session.profile, proposed, base, rated), ""
