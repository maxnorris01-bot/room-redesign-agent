"""The canned question writer for mock mode: free, deterministic, no network.

`mock_caller` has the signature of `app.llm.complete` and answers the clarify call the way a careful
model would: it reads the numbered listing rows in the user text (the same text the real model gets)
and builds questions from the traits whose values split the titles, skipping what the request states
and what nearly every title shares. It exists so the whole harness (timing fields, picture matching,
flags, persona answers, the pipeline arms, the report and the contact sheet) runs in CI. Mock numbers
show that the harness works, not how good the questions are.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm import Completion

DIMENSION_WORDS: dict[str, tuple[str, ...]] = {
    "color": ("cream", "black", "navy", "walnut", "oak", "brass", "grey", "gray", "white", "beige",
              "rust", "green", "blue", "tan", "natural", "brown", "gold", "silver"),
    "material": ("wood", "metal", "velvet", "boucle", "leather", "linen", "cotton", "wool", "jute",
                 "steel", "marble", "glass", "mesh", "laminate", "polyester", "upholstered"),
    "style": ("mid-century", "modern", "industrial", "boho", "farmhouse", "minimalist",
              "scandinavian", "traditional"),
    "shape": ("round", "rectangular", "oval", "curved", "square", "arched"),
}  # fmt: skip
QUESTION_TEXT = {
    "color": "Which colour do you like?",
    "material": "What should it be made of?",
    "style": "Which look do you prefer?",
    "shape": "Which shape do you want?",
    "size": "How big should it be?",
}
ROW = re.compile(r"^(\d+) \| (.*?) \| (\$[\d,]+|no price) \| (.*?) \| (.*)$")
SPLIT_MAX_SHARE = 0.9
MIN_OPTION_ITEMS = 3


def _rows(user_text: str) -> list[tuple[str, str, str]]:
    out = []
    for line in user_text.splitlines():
        m = ROW.match(line)
        if m:
            out.append((m.group(1), m.group(2).lower().replace("-", " "), m.group(5)))
    return out


def _stated(user_text: str) -> str:
    m = re.search(r"<request>(.*?)</request>", user_text)
    attrs = re.search(r"Stated attributes: (.*)", user_text)
    return f"{m.group(1) if m else ''} {attrs.group(1) if attrs else ''}".lower().replace("-", " ")


def questions_for(user_text: str) -> dict[str, Any]:
    rows = _rows(user_text)
    stated = _stated(user_text)
    total = len(rows)
    scored: list[tuple[float, dict[str, Any]]] = []
    for dim, vocab in DIMENSION_WORDS.items():
        counts: dict[str, list[str]] = {}
        for number, title, _size in rows:
            for word in vocab:
                if re.search(rf"\b{re.escape(word.replace('-', ' '))}\b", title):
                    counts.setdefault(word, []).append(number)
        usable = {
            w: n
            for w, n in counts.items()
            if len(n) >= MIN_OPTION_ITEMS and w.replace("-", " ") not in stated
        }
        if len(usable) < 2 or not total:
            continue
        top = sorted(usable.items(), key=lambda kv: -len(kv[1]))[:4]
        if len(top[0][1]) / total >= SPLIT_MAX_SHARE:
            continue
        balance = 1 - len(top[0][1]) / sum(len(n) for _, n in top)
        scored.append(
            (
                balance + 0.01 * len(top),
                {
                    "id": f"q_{dim}",
                    "text": QUESTION_TEXT[dim],
                    "kind": "choice",
                    "dimension": dim,
                    "options": [
                        {"label": w.title(), "value": w, "evidence_product_ids": n[:3]}
                        for w, n in top
                    ],
                },
            )
        )
    sized = [(n, s) for n, _t, s in rows if s != "no size"]
    if len(sized) >= 8:
        scored.append(
            (
                0.4,
                {
                    "id": "q_size",
                    "text": QUESTION_TEXT["size"],
                    "kind": "size_range",
                    "dimension": "size",
                    "options": [
                        {
                            "label": "Under 40 in",
                            "value": "under 40 in",
                            "evidence_product_ids": [],
                        },
                        {
                            "label": "40 to 60 in",
                            "value": "40 to 60 in",
                            "evidence_product_ids": [],
                        },
                        {"label": "Over 60 in", "value": "over 60 in", "evidence_product_ids": []},
                    ],
                },
            )
        )
    scored.sort(key=lambda s: -s[0])
    return {"questions": [q for _, q in scored[:5]]}


def mock_caller(**kwargs: Any) -> Completion:
    """`complete`'s signature; the clarify call only."""
    payload = questions_for(kwargs["user_text"])
    return Completion(
        text=json.dumps(payload), model=kwargs.get("model", "mock"), tool_input=payload,
        input_tokens=1500, output_tokens=300, cost_usd=0.0, steps=1, stop_reason="tool_use",
    )  # fmt: skip
