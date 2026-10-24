"""A mock Anthropic client, so the pipeline runs end-to-end with zero API calls.

Two things live here:

  * The response-construction primitives (`text_block`, `search_blocks`, `response`) and
    `ScriptedClient`, which replays pre-built responses in order. Use these in unit tests that
    need exact control over a specific scenario (a `pause_turn`, malformed output, a budget-cap
    trip) that an auto-generated response can't express.
  * `MockAnthropicClient`, which auto-generates a schema-valid response for *any* structured-output
    request by walking the request's `output_config.format.schema` and filling in a placeholder
    value for each property (the first `enum` value if the schema gives one, otherwise a
    type-appropriate placeholder). This is what `APP_LLM_MODE=mock` (the default - see
    `Config.llm_mode`) wires into `app.llm.get_client`, so every entry point runs for free by
    default, with zero real API calls.

`MockAnthropicClient` is deliberately NOT a stand-in for real model quality: values are picked
generically from the schema, not reasoned about from input. It's good for exercising routing,
schema shape, tracing, and budget accounting for free - not for checking whether an answer is
actually correct. Only a live run (the real API) tests that. If a project's mock needs to route
differently per input (the way a classifier does, so downstream routing can be exercised
structurally), add that logic here per-project - see docs/lessons-learned.md for a worked example
(a keyword-based router built for one project) to crib the shape from without copying its
domain-specific keywords.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

# ---- Response-construction primitives ------------------------------------------------------
# Shared by ScriptedClient and MockAnthropicClient below, and usable directly from tests that
# need exact control over a specific response.


# Deterministic fake time-to-first-token reported by every mock response (never a measurement).
MOCK_TTFT_SECONDS = 0.25


def text_block(payload: dict[str, Any] | str) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(type="text", text=text)


def search_blocks(query: str, urls: list[str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(type="server_tool_use", name="web_search", input={"query": query}),
        SimpleNamespace(
            type="web_search_tool_result", content=[SimpleNamespace(url=u) for u in urls]
        ),
    ]


def response(
    content: list[SimpleNamespace],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
    searches: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=SimpleNamespace(web_search_requests=searches),
        ),
        _request_id="req_mock",
        _ttft_seconds=MOCK_TTFT_SECONDS,
    )


class ScriptedClient:
    """Returns scripted responses in order and records every request. Unit-test use only.

    Unlike `MockAnthropicClient`, this has no logic of its own - it exists to let a test pin down
    an exact sequence of responses that the auto-generating client's always-succeeds happy path
    can't express.
    """

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._responses.pop(0)


# ---- Generic schema-driven placeholder generator ---------------------------------------------


def _placeholder_for(schema: dict[str, Any]) -> Any:
    """Build one JSON-schema-valid placeholder value.

    Handles the JSON Schema subset structured output actually uses: object/properties/required,
    array/items, string/enum, integer, number, boolean. Falls back to a bare string for anything
    else, which is usually enough to keep a request schema-valid even for a shape this hasn't
    seen.
    """
    if "enum" in schema:
        value: Any = schema["enum"][0]
        return value
    schema_type = schema.get("type", "string")
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", list(properties))
        return {name: _placeholder_for(properties.get(name, {})) for name in required}
    if schema_type == "array":
        item_schema = schema.get("items", {})
        return [_placeholder_for(item_schema)]
    if schema_type in ("integer", "number"):
        return 0
    if schema_type == "boolean":
        return False
    return "mock"


MOCK_RETAILER = "MOCK"
MOCK_URL_FOUND = "https://mock.example/product/found"
MOCK_URL_UNVERIFIED = "https://mock.example/product/never-searched"


def mock_perception_payload() -> dict[str, Any]:
    """A schema-valid, obviously fake room (title-routed from `RoomPerception`)."""
    return {
        "schema_version": 1,
        "room_type": "MOCK living room",
        "dimensions": {
            "width_m": 4.0,
            "length_m": 5.0,
            "ceiling_height_m": 2.5,
            "confidence": 0.1,
            "derivation": "MOCK placeholder, not derived from the image",
        },
        "openings": [
            {"kind": "window", "wall": "east", "position_along_wall": 0.5, "width_m": 1.2,
             "note": "MOCK window"},
            {"kind": "door", "wall": "south", "position_along_wall": 0.2, "width_m": 0.9,
             "note": "MOCK door"},
        ],
        "fixed_items": [
            {"category": "radiator", "position": {"x": 0.9, "y": 0.5},
             "size": {"width_m": 0.2, "depth_m": 1.0}, "note": "MOCK fixed item"},
        ],
        "furniture": [
            {"id": "f1", "category": "sofa", "position": {"x": 0.3, "y": 0.6},
             "size": {"width_m": 2.0, "depth_m": 0.9}, "keepable": "unlikely",
             "note": "MOCK warm ivory sofa, marked replaceable", "matches_id": None},
            {"id": "f2", "category": "armchair", "position": {"x": 0.7, "y": 0.7},
             "size": {"width_m": 0.9, "depth_m": 0.9}, "keepable": "likely",
             "note": "MOCK pale blue armchair, matches sofa f1", "matches_id": "f1"},
            {"id": "f3", "category": "desk", "position": {"x": 0.8, "y": 0.2},
             "size": {"width_m": 1.4, "depth_m": 0.7}, "keepable": "likely",
             "note": "MOCK desk, marked keepable", "matches_id": None},
        ],
        "furniture_gaps": [
            {"category": "coffee table", "reason": "MOCK: bare floor in front of sofa"},
        ],
        "living_things": [
            {"kind": "pet", "description": "MOCK pet, never furniture",
             "position": {"x": 0.5, "y": 0.8}},
        ],
        "art_and_decor": [
            {"kind": "wall art", "location": "north wall", "note": "MOCK decor"},
        ],
        "style_descriptors": ["mock modern", "mock warm wood", "mock bright"],
        "uncertainties": ["MOCK: placeholder data, not a real room"],
    }  # fmt: skip


def mock_retrieval_payload(slot: str) -> dict[str, Any]:
    """Clearly labelled fake candidates; one URL is deliberately absent from the search results,
    so the unverified-URL guard visibly drops it in every mock run."""
    return {
        "slot": slot,
        "candidates": [
            {"name": f"MOCK {slot} (fake)", "retailer": MOCK_RETAILER, "price": 123.45,
             "currency": "USD", "url": MOCK_URL_FOUND},
            {"name": f"MOCK {slot} (fake, URL not in search results)", "retailer": MOCK_RETAILER,
             "price": 99.0, "currency": "USD", "url": MOCK_URL_UNVERIFIED},
        ],
        "note": "MOCK data from the mock client; nothing here is a real product",
    }  # fmt: skip


MOCK_INVENTED_ID = "mock-invented-id"


def _shop_mock_order(request_text: str, ids: list[str]) -> list[str]:
    """Shop Mode mock ranking: results whose titles share words with the wanted terms first (more
    shared words first), ties in list order; avoided and disliked words sink. Wanted terms are the
    request's style, materials and must-haves and, for a Shop Again, the profile's stated and
    inferred likes (never the parser's style guesses), plus a boost for the favoured seller and for
    a price inside the price band."""

    def words(pattern: str) -> set[str]:
        found = re.search(rf"^{pattern}: (.*)$", request_text, flags=re.MULTILINE)
        text = found.group(1) if found and found.group(1) != "none" else ""
        return set(re.findall(r"[a-z]+", text.lower()))

    want = words("Style") | words("Materials") | words("Must have") | words(r"Profile likes[^:\n]*")
    converged = words(r"Converged preferences[^:\n]*") - {
        "shape",
        "material",
        "finish",
        "color",
        "frame",
    }
    avoid = words("Avoid") | words(r"Profile dislikes[^:\n]*") | words(r"Inferred dislikes")
    if "dark" in avoid:  # "too dark" in a profile: the mock reads it as these colours
        avoid |= {"black", "navy", "charcoal"}
    seller = re.search(r"^Seller the shopper likes: (.*)$", request_text, flags=re.MULTILINE)
    favoured = seller.group(1).strip().lower() if seller and seller.group(1) != "none" else ""
    band = re.search(r"^Price band[^:\n]*: .*?\$([\d,]+) to \$([\d,]+)", request_text, flags=re.M)
    low, high = (
        (float(band.group(1).replace(",", "")), float(band.group(2).replace(",", "")))
        if band
        else (None, None)
    )
    rows = {
        m[0]: m
        for m in re.findall(
            r"^- id: (\S+) \| (.*?) \| seller: (.*?) \| USD ([\d.]+|no price)",
            request_text,
            flags=re.M,
        )
    }

    def score(i: str) -> int:
        _, title, who, price = rows.get(i, (i, "", "", ""))
        got = set(re.findall(r"[a-z]+", title.lower()))
        points = len(want & got) + 4 * len(converged & got) - 5 * len(avoid & got)
        if favoured and who.strip().lower() == favoured:
            points += 3
        if low is not None and high is not None and price.replace(".", "").isdigit():
            points += 2 if low <= float(price) <= high else 0
        return points

    return sorted(ids, key=lambda i: -score(i))  # sorted is stable


def mock_rank_payload(request_text: str) -> dict[str, Any]:
    """Finalists for a rank request: the first N real ids from its list, plus one invented id.

    The invented id is deliberate: the id-validation step visibly drops it in every mock run.
    """
    ids = re.findall(r"^- id: (\S+) \|", request_text, flags=re.MULTILINE)
    match = re.search(r"Pick at most (\d+) finalists", request_text)
    n = int(match.group(1)) if match else 2
    if request_text.startswith("Shopper request:"):  # Shop Mode: order by the soft preferences
        ids = _shop_mock_order(request_text, ids)
    finalists = [{"id": i, "reason": "MOCK pick: first on the list"} for i in ids[:n]]
    finalists.append({"id": MOCK_INVENTED_ID, "reason": "MOCK: id not in the candidate list"})
    return {"finalists": finalists}


# ---- proposal and style-check mocks, plus builders for scripting them in tests -----------------


def proposal_payload(
    picks: dict[str, str | None],
    *,
    alternates: dict[str, list[str]] | None = None,
    summary: str = "MOCK summary",
) -> dict[str, Any]:
    """A ProposalOutput payload: `picks` maps slot name to a finalist id (None = leave empty)."""
    alternates = alternates or {}
    return {
        "slots": [
            {
                "slot": slot,
                "pick_id": pick or "",
                "alternate_ids": alternates.get(slot, []),
                "rationale": f"MOCK rationale for {slot}",
                "fit_note": "MOCK fit note",
            }
            for slot, pick in picks.items()
        ],
        "summary": summary,
    }


def style_payload(
    matches: dict[str, str],
    note: str = "MOCK style note",
    clash: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A StyleCheckOutput payload: `matches` maps slot name to good, ok or poor; `clash` maps slot
    name to none, mild or strong (a clash is with "armchair (f2)", the mock's kept armchair)."""
    clash = clash or {}
    items = []
    for slot, match in matches.items():
        level = clash.get(slot, "none")
        items.append(
            {
                "slot": slot,
                "match": match,
                "note": note,
                "clash": level,
                "clash_with": "" if level == "none" else "armchair (f2)",
            }
        )
    return {"items": items}


def mock_proposal_payload(request_text: str) -> dict[str, Any]:
    """Deterministic proposal from the request text.

    First pass: per slot, the first finalist with a retailer link (else the first), the next one as
    an alternate, plus one invented alternate id that validation visibly drops. Revision pass (the
    text has "Failed checks:"): the finalist with the lowest total, so an over-budget proposal gets
    cheaper. A slot with no finalists is left empty.
    """
    revising = "Failed checks:" in request_text
    slots: list[dict[str, Any]] = []
    blocks = re.split(r"^Slot: ", request_text, flags=re.MULTILINE)[1:]
    for index, block in enumerate(blocks):
        name = block.splitlines()[0].strip()
        rows = re.findall(
            r"^- id: (\S+) \|.*?\| total ([\d.]+) \|.*?\| link: (\w+) \|",
            block.split("Previous proposal:")[0],
            flags=re.MULTILINE,
        )
        ids = [r[0] for r in rows]
        if not rows:
            pick, alts = "", []
        elif revising:
            ordered = sorted(rows, key=lambda r: float(r[1]))
            pick, alts = ordered[0][0], [r[0] for r in ordered[1:2]]
        else:
            retailer = [r for r in rows if r[2] == "retailer"]
            pick = (retailer or rows)[0][0]
            alts = [i for i in ids if i != pick][:1]
        if index == 0 and rows and not revising:
            alts = [*alts, MOCK_INVENTED_ID]
        slots.append(
            {
                "slot": name,
                "pick_id": pick,
                "alternate_ids": alts,
                "rationale": f"MOCK {'revised ' if revising else ''}pick for {name}",
                "fit_note": "MOCK fit note",
            }
        )
    summary = "MOCK summary: deterministic placeholder proposal."
    if "Set note:" in request_text:  # the summary must acknowledge a matching kept piece
        summary += " It acknowledges a kept matching piece that may clash with the new item."
    return {"slots": slots, "summary": summary}


def mock_style_payload(request_text: str, *, v2: bool = True) -> dict[str, Any]:
    """Every listed item "good" with no clash, unless its title contains POOR-STYLE (poor) or
    CLASH (a strong clash with the first kept item): hooks for tests. v1 has no clash fields."""
    kept = re.search(r"Furniture that STAYS: (.+)", request_text)
    first_kept = ""
    if kept and kept.group(1).strip() != "nothing":
        first_kept = re.match(r"([^:;]+\([^)]*\))", kept.group(1)).group(1)  # type: ignore[union-attr]
    items = []
    for line in request_text.splitlines():
        match = re.match(r"^- slot: (.+?) \| (.*?) \| retailer:", line)
        if not match:
            continue
        title = match.group(2)
        item: dict[str, Any] = {
            "slot": match.group(1),
            "match": "poor" if "POOR-STYLE" in title else "good",
            "note": "MOCK style note",
        }
        if v2:
            strong = "CLASH" in title and bool(first_kept)
            item["clash"] = "strong" if strong else "none"
            item["clash_with"] = first_kept if strong else ""
        items.append(item)
    return {"items": items}


# ---- Shop Mode: request parsing --------------------------------------------------------------


def _sample(
    search_text: str, category: str, price_min: float | None = None, price_max: float | None = None,
    style: tuple[str, ...] = (), material: tuple[str, ...] = (), must: tuple[str, ...] = (),
    avoid: tuple[str, ...] = (),
) -> dict[str, Any]:  # fmt: skip
    return {
        "search_text": search_text, "category": category,
        "price_min": price_min, "price_max": price_max,
        "style_terms": list(style), "material_terms": list(material),
        "must_have": list(must), "avoid": list(avoid),
    }  # fmt: skip


# What the mock "model" answers for known requests (lowercase, punctuation-free keys). Anything
# else gets `_heuristic_shop_parse`. Mock output is not a measure of parse quality.
MOCK_SHOP_PARSES: dict[str, dict[str, Any]] = {
    "modern couches under 1000": _sample(
        "modern couch", "couch", price_max=1000.0, style=("modern",)
    ),
    "coffee tables with a mix of wood and metal with storage": _sample(
        "wood and metal coffee table with storage",
        "coffee table",
        style=("industrial",),  # a parser guess: the request never says it (see guess_style_terms)
        material=("wood", "metal"),
        must=("storage",),
    ),
    "boho curtains": _sample("boho curtains", "curtains", style=("boho",)),
    "mid century walnut sideboard between 400 and 900": _sample(
        "mid century walnut sideboard",
        "sideboard",
        400.0,
        900.0,
        style=("mid-century",),
        material=("walnut",),
    ),
    "cozy velvet armchair no leather under 350": _sample(
        "velvet armchair",
        "armchair",
        price_max=350.0,
        style=("cozy",),
        material=("velvet",),
        avoid=("leather",),
    ),
    "brass floor lamp for a small bedroom": _sample(
        "brass floor lamp", "floor lamp", material=("brass",), must=("small footprint",)
    ),
    "scandinavian dining table that seats 6 over 500": _sample(
        "scandinavian dining table seats 6",
        "dining table",
        price_min=500.0,
        style=("scandinavian",),
        must=("seats 6",),
    ),
    "washable area rug for a kids room": _sample(
        "washable area rug", "rug", must=("washable",), style=("playful",)
    ),
}


def _norm_request(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("-", " ")).split())


def _heuristic_shop_parse(request: str) -> dict[str, Any]:
    """A crude parse for requests outside `MOCK_SHOP_PARSES`: the words as typed, plus a price."""
    low = request.lower().replace(",", "")
    under = re.search(r"(?:under|below|less than|max)\s*\$?(\d+)", low)
    over = re.search(r"(?:over|above|at least|min)\s*\$?(\d+)", low)
    words = re.sub(r"(?:under|below|less than|max|over|above|at least|min)\s*\$?\d+", " ", low)
    text = " ".join(words.split()) or "furniture"
    return _sample(
        text,
        text.split()[-1] if text.split() else "",
        float(over.group(1)) if over else None,
        float(under.group(1)) if under else None,
    )


def mock_shop_parse_payload(request_text: str) -> dict[str, Any]:
    found = re.search(r"<request>(.*?)</request>", request_text, flags=re.DOTALL)
    request = found.group(1) if found else request_text
    return MOCK_SHOP_PARSES.get(_norm_request(request)) or _heuristic_shop_parse(request)


# ---- Shop Mode: the preference profile ---------------------------------------------------------

# Attribute words the mock "model" recognises in titles and comments (it is not a language model).
MOCK_PREF_WORDS = (
    "boucle", "velvet", "linen", "leather", "chenille", "walnut", "oak", "rattan", "marble",
    "glass", "metal", "wood", "low profile", "tufted", "curved", "round", "storage", "mid-century",
    "minimalist", "industrial", "farmhouse",
)  # fmt: skip
_PREF_ROW = re.compile(r"^- (love|like|dislike) \| (.*?) \| (.*?) \| (.*?) \| comment: (.*)$")


def mock_prefs_payload(request_text: str) -> dict[str, Any]:
    """A profile from the rated rows: attributes of loved results (and of liked ones when two
    share them), "too X" comments as dislikes, an attribute shared by two dislikes and no like as a
    dislike, "too expensive" as a 15% lower cap. The existing profile is carried over."""
    rows = [m.groups() for m in map(_PREF_ROW.match, request_text.splitlines()) if m]
    weight: dict[str, int] = {}
    disliked: dict[str, int] = {}
    dislikes: list[str] = []
    cheaper = False
    for rating, title, _price, _seller, comment in rows:
        text = f"{title} {comment}".lower()
        found = [w for w in MOCK_PREF_WORDS if w in text]
        for w in found:
            if rating == "dislike":
                disliked[w] = disliked.get(w, 0) + 1
            else:
                weight[w] = weight.get(w, 0) + (2 if rating == "love" else 1)
        for m in re.finditer(r"too (\w+)", comment.lower()):
            if m.group(1) in ("expensive", "pricey", "costly"):
                cheaper = True
            else:
                dislikes.append(f"too {m.group(1)}")
        if "flimsy" in comment.lower():
            dislikes.append("flimsy legs")
    likes = [w for w, n in weight.items() if n >= 2]
    dislikes += [w for w, n in disliked.items() if n >= 2 and w not in weight]

    def carried(label: str) -> list[str]:
        found = re.search(rf"^  {label}: (.*)$", request_text, flags=re.MULTILINE)
        text = found.group(1) if found else "none"
        return [] if text == "none" else [x.strip() for x in text.split(";") if x.strip()]

    likes = [*carried("likes"), *[x for x in likes if x not in carried("likes")]]
    dislikes = [*carried("dislikes"), *[x for x in dislikes if x not in carried("dislikes")]]
    base = re.search(r"^Parsed query: search_text=(.*?);", request_text, flags=re.MULTILINE)
    search_text = base.group(1).strip() if base else ""
    extra = [x for x in likes if x not in search_text]
    cap = re.search(r"price=up to \$([\d,]+)", request_text)
    price_max = float(cap.group(1).replace(",", "")) * 0.85 if cheaper and cap else None
    return {
        "likes": likes,
        "dislikes": dislikes,
        "query_adjustments": {
            "search_text": f"{search_text} {extra[0]}".strip() if extra and search_text else "",
            "style_terms": [
                x for x in likes if x in ("mid-century", "minimalist", "industrial", "farmhouse")
            ],
            "material_terms": [
                x
                for x in likes
                if x
                in (
                    "boucle",
                    "velvet",
                    "linen",
                    "leather",
                    "walnut",
                    "oak",
                    "rattan",
                    "marble",
                    "glass",
                    "metal",
                    "wood",
                )
            ],
            "must_have": [x for x in likes if x in ("storage", "low profile")],
            "avoid": ["dark"] if "too dark" in dislikes else [],
            "price_max": price_max,
            "price_note": "Lowered the price limit about 15% because of a comment."
            if price_max
            else "",
        },
    }


MOCK_ATTR_PHRASES = (
    ("brushed brass", "color_finish"), ("solid walnut", "quality_cue"),
    ("solid wood", "quality_cue"),
    ("brass", "material"), ("walnut", "material"), ("boucle", "material"), ("velvet", "material"),
    ("oak", "material"), ("rattan", "material"), ("marble", "material"), ("glass", "material"),
    ("lift-top", "shape"), ("drum", "shape"), ("round", "shape"), ("low profile", "shape"),
    ("tufted", "shape"), ("curved", "shape"), ("metal legs", "frame_or_legs"),
    ("metal frame", "frame_or_legs"),
)  # fmt: skip
_PREF_ROW_V2 = re.compile(
    r"^- (love|like|dislike) \| id: (\S+) \| (.*?) \| (\$[\d,]+|no price) \| (.*?) \| "
    r"comment: (.*?) \| (image \d+|image none)$"
)


def _phrases_in(title: str) -> list[tuple[str, str]]:
    """The most specific MOCK_ATTR_PHRASES in a title (one inside a longer match is dropped)."""
    low = title.lower()
    found = [(p, k) for p, k in MOCK_ATTR_PHRASES if p in low]
    return [(p, k) for p, k in found if not any(p != q and p in q for q, _ in found)]


def mock_prefs_v2_payload(request_text: str) -> dict[str, Any]:
    """A v2 profile read from the rated rows, deterministic. Stated likes and dislikes come from
    comments only. Inferred attributes are the MOCK_ATTR_PHRASES found in loved or liked titles (as
    likes) and disliked titles (as dislikes), each with the ids that show it; the mock proposes
    single-item attributes too, so the rules in app.shop.prefs are what filters them. Sellers and a
    price band are proposed (the code ignores them). When two or more loved or liked rows come with
    an image, a visual "premium metal finish" cue is added. A parser style guess that no loved or
    liked title shows is reported as contradicted when something is loved."""
    rows = [m.groups() for m in map(_PREF_ROW_V2.match, request_text.splitlines()) if m]
    likes: list[str] = []
    dislikes: list[str] = []
    cheaper = False
    evidence: dict[tuple[str, str, str], list[str]] = {}
    for rating, rid, title, _price, _seller, comment, _img in rows:
        pol = "dislike" if rating == "dislike" else "like"
        for phrase, kind in _phrases_in(title):
            evidence.setdefault((phrase, kind, pol), []).append(rid)
        low = comment.lower()
        if rating != "dislike":
            likes += [p for p, _ in MOCK_ATTR_PHRASES if p in low and p not in likes]
        for m in re.finditer(r"too (\w+)", low):
            if m.group(1) in ("expensive", "pricey", "costly"):
                cheaper = True
            elif f"too {m.group(1)}" not in dislikes:
                dislikes.append(f"too {m.group(1)}")
        if "flimsy" in low and "flimsy legs" not in dislikes:
            dislikes.append("flimsy legs")

    def carried(label: str) -> list[str]:
        found = re.search(rf"^  {label}: (.*)$", request_text, flags=re.MULTILINE)
        text = found.group(1) if found else "none"
        return [] if text == "none" else [x.strip() for x in text.split(";") if x.strip()]

    likes = [*carried("likes"), *[x for x in likes if x not in carried("likes")]]
    dislikes = [*carried("dislikes"), *[x for x in dislikes if x not in carried("dislikes")]]
    inferred: list[dict[str, Any]] = [
        {"kind": kind, "text": phrase, "polarity": pol, "evidence": ids,
         "confidence": round(min(0.95, 0.4 + 0.2 * len(ids)), 2)}
        for (phrase, kind, pol), ids in evidence.items()
    ]  # fmt: skip
    positive = [r for r in rows if r[0] != "dislike"]
    with_image = [r[1] for r in positive if r[6] != "image none"]
    if len(with_image) >= 2:
        inferred.append({"kind": "quality_cue", "text": "premium metal finish", "polarity": "like",
                         "evidence": with_image[:4], "confidence": 0.6})  # fmt: skip
    for who in dict.fromkeys(r[4] for r in positive):
        ids = [r[1] for r in positive if r[4] == who]
        inferred.append({"kind": "seller", "text": f"seller: {who}", "polarity": "like",
                         "evidence": ids, "confidence": 0.9})  # fmt: skip
    if positive:
        inferred.append({"kind": "price_band", "text": "upper price", "polarity": "like",
                         "evidence": [r[1] for r in positive], "confidence": 0.5})  # fmt: skip
    guess_line = re.search(
        r"^Style guessed by the request parser[^:\n]*: (.*)$", request_text, flags=re.M
    )
    guesses = [g.strip() for g in guess_line.group(1).split(",")] if guess_line else []
    loved = [r for r in rows if r[0] == "love"]
    contradicted = [
        {"text": g, "evidence": [r[1] for r in loved][:2]}
        for g in guesses
        if g
        and g != "none"
        and loved
        and not any(w in r[2].lower() for r in positive for w in re.findall(r"[a-z]+", g.lower()))
    ]
    cap = re.search(r"price=up to \$([\d,]+)", request_text)
    price_max = float(cap.group(1).replace(",", "")) * 0.85 if cheaper and cap else None
    return {
        "likes": likes,
        "dislikes": dislikes,
        "inferred": inferred,
        "contradicted_guesses": contradicted,
        "query_adjustments": {
            "search_text": "", "style_terms": [], "material_terms": [], "must_have": [],
            "avoid": ["dark"] if "too dark" in dislikes else [],
            "price_max": price_max,
            "price_note": "Lowered the price limit about 15% because of a comment."
            if price_max
            else "",
        },
    }  # fmt: skip


def mock_prefs_v3_payload(request_text: str) -> dict[str, Any]:
    """A v3 answer: per-item tags read from title keywords (the shopper's own words from comments,
    parser-guess contradictions and price asks come from the v2 mock). It describes only; it never
    says what the shopper prefers. A visual cue ("visible grain") is added only for an item that
    came with an image, so a run with no images differs from one with them."""
    from app.shop.decide import lexicon_match

    base = mock_prefs_v2_payload(request_text)
    items: list[dict[str, Any]] = []
    for line in request_text.splitlines():
        m = _PREF_ROW_V2.match(line)
        if not m:
            continue
        _rating, rid, title, _price, _seller, _comment, image = m.groups()
        low = title.lower()

        def raw(dim: str, text: str = title) -> str | None:
            found = lexicon_match(dim, text)
            return found[1] if found else None

        cues: list[str] = []
        if "solid" in low:
            cues.append("solid wood")
        if "brushed brass" in low:
            cues.append("brushed finish")
        if "marble" in low:
            cues.append("stone top")
        if image != "image none" and ("walnut" in low or "oak" in low or "wood" in low):
            cues.append("visible grain")
        shape = raw("shape")
        canon = lexicon_match("shape", title)
        items.append(
            {
                "id": rid,
                "shape": canon[0] if canon and shape else None,
                "material": raw("material"),
                "color_finish": raw("color_finish"),
                "frame_or_legs": raw("frame_or_legs"),
                "frame_color": raw("frame_color"),
                "style": raw("style"),
                "size_class": None,
                "quality_cues": cues,
            }
        )
    return {
        "items": items,
        "likes": base["likes"],
        "dislikes": base["dislikes"],
        "contradicted_guesses": base["contradicted_guesses"],
        "query_adjustments": base["query_adjustments"],
    }


def _request_text(kwargs: dict[str, Any]) -> str:
    content = kwargs["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if b.get("type") == "text")


def _slot_from_request(kwargs: dict[str, Any]) -> str:
    """The retrieval user message names its slot on a line starting 'Slot: '."""
    content = kwargs["messages"][0]["content"]
    text = (
        content
        if isinstance(content, str)
        else " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    )
    for line in text.splitlines():
        if line.startswith("Slot: "):
            return line.removeprefix("Slot: ").strip()
    return "unknown slot"


class MockAnthropicClient:
    """Auto-generates a schema-valid response for any structured-output request.

    Wired in by `app.llm.get_client` when `Config.llm_mode == "mock"` (the default). Inspects the
    request's `output_config.format.schema` and fills it in generically via `_placeholder_for` -
    good enough to exercise routing/shape/tracing/budget accounting for any schema, with no
    project-specific logic needed. If a project's mock needs to route differently based on the
    input (e.g. a classifier that should pick different labels for different inputs so downstream
    routing can be exercised structurally), add that per-project rather than here - see
    docs/lessons-learned.md.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        schema = kwargs.get("output_config", {}).get("format", {}).get("schema")
        if schema is None:
            # No structured-output schema on this request - just echo something schema-free.
            return response([text_block("mock response")], input_tokens=100, output_tokens=20)
        title = schema.get("title")
        if title == "ProposalOutput":
            return response(
                [text_block(mock_proposal_payload(_request_text(kwargs)))],
                input_tokens=1500,
                output_tokens=300,
            )
        if title in ("StyleCheckOutput", "StyleCheckOutputV1"):
            images = (
                sum(
                    1
                    for b in kwargs["messages"][0]["content"]
                    if isinstance(b, dict) and b.get("type") == "image"
                )
                if not isinstance(kwargs["messages"][0]["content"], str)
                else 0
            )
            return response(
                [
                    text_block(
                        mock_style_payload(_request_text(kwargs), v2=title == "StyleCheckOutput")
                    )
                ],
                input_tokens=300 + 150 * images,
                output_tokens=60,
            )
        if title == "PreferenceTags":
            content = kwargs["messages"][0]["content"]
            n_images = (
                0
                if isinstance(content, str)
                else sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
            )
            return response(
                [text_block(mock_prefs_v3_payload(_request_text(kwargs)))],
                input_tokens=1200 + 90 * n_images,
                output_tokens=500,
            )
        if title == "PreferenceProfileV2":
            images = (
                sum(
                    1
                    for b in kwargs["messages"][0]["content"]
                    if isinstance(b, dict) and b.get("type") == "image"
                )
                if not isinstance(kwargs["messages"][0]["content"], str)
                else 0
            )
            return response(
                [text_block(mock_prefs_v2_payload(_request_text(kwargs)))],
                input_tokens=1200 + 90 * images,
                output_tokens=350,
            )
        if title == "PreferenceProfile":
            return response(
                [text_block(mock_prefs_payload(_request_text(kwargs)))],
                input_tokens=900,
                output_tokens=150,
            )
        if title == "ShopParse":
            return response(
                [text_block(mock_shop_parse_payload(_request_text(kwargs)))],
                input_tokens=350,
                output_tokens=90,
            )
        if title == "RenderLocateOutput":
            slots = [
                line.removeprefix("Item: ").split(" - ")[0]
                for line in _request_text(kwargs).splitlines()
                if line.startswith("Item: ")
            ]
            boxes = [
                {"slot": s, "placement": f"MOCK place for {s}",
                 "box": {"x0": 0.1 + 0.25 * (i % 3), "y0": 0.3,
                         "x1": 0.3 + 0.25 * (i % 3), "y1": 0.8}}
                for i, s in enumerate(slots)
            ]  # fmt: skip
            return response(
                [text_block(json.dumps({"items": boxes}))], input_tokens=900, output_tokens=120
            )
        if title == "RenderJudgeOutput":
            slots = [
                line.removeprefix("Item: ").split(" (")[0]
                for line in _request_text(kwargs).splitlines()
                if line.startswith("Item: ")
            ]
            judged = {
                "items": [{"slot": s, "look_alike": 3, "notes": "MOCK note"} for s in slots],
                "rest_of_room_changed": False,
                "room_notes": "MOCK: nothing noticeable",
            }
            return response([text_block(json.dumps(judged))], input_tokens=1500, output_tokens=120)
        if title == "RankedFinalists":
            return response(
                [text_block(mock_rank_payload(_request_text(kwargs)))],
                input_tokens=900,
                output_tokens=80,
            )
        if title == "RoomPerception":
            return response(
                [text_block(mock_perception_payload())], input_tokens=1800, output_tokens=700
            )
        if title == "SlotCandidates":
            slot = _slot_from_request(kwargs)
            return response(
                [
                    *search_blocks(f"MOCK query: {slot}", [MOCK_URL_FOUND]),
                    text_block(mock_retrieval_payload(slot)),
                ],
                input_tokens=2500,
                output_tokens=250,
                searches=1,
            )
        payload = _placeholder_for(schema)
        uses_search = bool(kwargs.get("tools"))
        if uses_search:
            content = [
                *search_blocks("mock query", ["https://mock.example/source"]),
                text_block(payload),
            ]
            return response(content, input_tokens=300, output_tokens=150, searches=1)
        return response([text_block(payload)], input_tokens=200, output_tokens=60)
