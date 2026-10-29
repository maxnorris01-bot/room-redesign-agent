"""Stage 2q, phase 5: the decidable-attribute line of the eval report, and the re-run record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.shop import report
from evals.shop.spec import thresholds_sha256

ROOT = Path(__file__).resolve().parents[1]


def items(yes: int, no: int, unsure: int) -> list[dict[str, Any]]:
    return [{"attributes": a} for a, n in (("yes", yes), ("no", no), ("cannot_tell", unsure))
            for _ in range(n)]  # fmt: skip


def test_the_rug_case_shows_10_of_12_next_to_the_verdict_number() -> None:
    got = report.decidable(items(10, 2, 12))
    assert (got["good"], got["no"], got["cannot_tell"]) == (10, 2, 12)
    assert round(got["rate"], 2) == 0.83
    data = {"cases": [{"id": "case-06", "rounds": [
        {"items": items(10, 2, 12), "attributes": {"rate": 10 / 24}},
    ]}]}  # fmt: skip
    text = "\n".join(report.decidable_section(data))
    assert "case-06: 10 of 12 (83%)" in text and "12 cannot_tell" in text
    assert "counts it as 42%" in text and "Overall (round 1): 10 of 12" in text


def test_no_decidable_items_gives_no_rate() -> None:
    assert report.decidable(items(0, 0, 5))["rate"] is None
    assert "No item data" in "\n".join(report.decidable_section({"cases": [{"id": "x"}]}))


def test_the_thresholds_are_untouched() -> None:
    assert thresholds_sha256() == "c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237"


def test_the_docs_say_the_figure_is_secondary_and_why() -> None:
    for name in ("README.md", "known_instability.md"):
        text = (ROOT / "evals" / "shop" / name).read_text()
        assert "decidable" in text and "cannot_tell" in text and "secondary" in text


def test_the_rerun_record_names_both_runs_and_the_caveat() -> None:
    text = (ROOT / "docs" / "evals" / "shop-eval-rerun-2026-10-28.md").read_text()
    for run in ("20261028-135526-320963", "20261028-140756-e4c419"):
        assert run in text
    assert "not the 12-case run" in text and "does not replace the baseline" in text
    assert "improved-cases" in text and json.dumps(0.0562)[:5] in text
