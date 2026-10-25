"""Turn a Shop Mode session file into a redacted replay fixture (ADR 0012, stage 2i).

From the repository root:

    uv run python scripts/shop_export_replay.py shop-data/ID.json tests/fixtures/shop/NAME.json

The output is in the format of `tests/fixtures/shop/owner_session_replay_*.json`: the typed
request, the rated items with the attributes the session's tags gave them, the other shown
results, and a candidate pool from the session's last pool snapshot (at most 100 items). Nothing
identifying is kept: the session id and timestamps are dropped, ids are renumbered, thumbnails
become placeholders under `https://mock.example/`, and a comment goes through the redaction tool
in `app.shop.redact` (keys, tokens) and then loses emails, phone numbers and links. Nothing here
calls a provider or reads `.env`. A session saved before stage 2i has no pool snapshot: the export
says so and writes an empty pool.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.shop.redact import redact  # noqa: E402
from app.shop.retailers import canonical_name  # noqa: E402

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
MAX_POOL = 100
DIMENSIONS = ("shape", "material", "color_finish", "frame_color", "style")


def scrub(text: str, limit: int = 160) -> str:
    """Redact keys and tokens, then emails, phone numbers and links; cut to `limit` characters."""
    cleaned = redact(" ".join(text.split()))
    for pattern in (_EMAIL, _URL, _PHONE):
        cleaned = pattern.sub("[removed]", cleaned)
    return cleaned[:limit]


def attrs_of(tags: dict[str, Any] | None) -> dict[str, str | None]:
    """The five attributes of a tag dict; a style with two labels keeps the first."""
    tags = tags or {}
    out: dict[str, str | None] = {}
    for dim in DIMENSIONS:
        value = tags.get(dim)
        out[dim] = (str(value).split(",")[0] or None) if value else None
    return out


def export(session_path: Path, out_path: Path) -> dict[str, Any]:
    """Write the fixture for `session_path` to `out_path` and return it."""
    session = json.loads(session_path.read_text())
    searches = session.get("searches", [])
    typed = [s for s in searches if s.get("kind", "search") == "search"]
    request = scrub((typed[-1] if typed else {"request": ""}).get("request", ""), 200)
    results: dict[str, dict[str, Any]] = session.get("results", {})
    ratings: dict[str, dict[str, Any]] = session.get("ratings", {})
    tag_cache: dict[str, dict[str, Any]] = session.get("tag_cache", {})
    rated_ids = sorted(
        (rid for rid, rec in ratings.items() if rec.get("rating") and rid in results),
        key=lambda rid: ratings[rid].get("rated_at", ""),
    )
    rated = []
    for n, rid in enumerate(rated_ids, start=1):
        r, rec = results[rid], ratings[rid]
        rated.append(
            {
                "id": f"E{n}", "title": scrub(r.get("title", ""), 140),
                "price": r.get("price") or 0.0, "seller": canonical_name(r.get("seller", "")),
                "rating": rec["rating"], "comment": scrub(rec.get("comment", "")),
                "attrs": attrs_of(tag_cache.get(rid)),
            }
        )  # fmt: skip
    extras = [
        {
            "id": f"X{n}",
            "title": scrub(results[rid].get("title", ""), 140),
            "price": results[rid].get("price") or 0.0,
            "seller": canonical_name(results[rid].get("seller", "")),
        }
        for n, rid in enumerate((r for r in results if r not in rated_ids), start=1)
    ]
    snapshots = session.get("pool_snapshots", [])
    items = (snapshots[-1]["items"] if snapshots else [])[:MAX_POOL]
    pool = [
        {
            "id": f"P{n}",
            "title": scrub(i.get("title", ""), 140),
            "price": i.get("price") or 0.0,
            "seller": canonical_name(i.get("seller", "")),
            "thumbnail": f"https://mock.example/pool/{n}.jpg",
        }
        for n, i in enumerate(items, start=1)
    ]
    if not pool:
        print("note: this session has no pool snapshot (saved before stage 2i): the pool is empty")
    data = {"request": request, "rated": rated, "shown_extras": extras, "pool": pool}
    out_path.write_text(json.dumps(data, indent=1) + "\n")
    return data


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    session_path, out_path = Path(argv[1]), Path(argv[2])
    if not session_path.is_file():
        print(f"no such session file: {session_path}")
        return 1
    data = export(session_path, out_path)
    print(
        f"wrote {out_path}: {len(data['rated'])} rated, {len(data['shown_extras'])} other shown, "
        f"{len(data['pool'])} in the pool"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
