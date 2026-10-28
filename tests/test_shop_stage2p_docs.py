"""Stage 2p: the thresholds and the baseline are untouched, and the docs say what changed."""

from __future__ import annotations

import hashlib
from pathlib import Path

from evals.shop.spec import thresholds_sha256

ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = "c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237"


def test_the_thresholds_are_exactly_the_ones_of_the_baseline_run() -> None:
    path = ROOT / "evals" / "shop" / "thresholds.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == BASELINE_SHA
    assert thresholds_sha256() == BASELINE_SHA


def test_the_rejudge_result_is_recorded_in_both_places() -> None:
    baseline = (ROOT / "docs" / "evals" / "shop-eval-baseline-2026-10-26.md").read_text()
    known = (ROOT / "evals" / "shop" / "known_instability.md").read_text()
    for text in (baseline, known):
        assert "20261027-041854-b00d9f" in text or "same run" in text
        assert "781 items" in text and "$0.0256" in text
        assert "90%" in text and "93%" in text and "3.6 points" in text and "6.1 points" in text
        assert "case-12, case-04, case-08, case-09 and case-11" in text and "85 to 88%" in text
        assert "1.8 points" in text and "not judge noise" in text
    # the baseline's own numbers stay as they were
    assert (
        "| lift mean" in baseline.lower()
        or "Shop Again lift, round 2 minus round 1 (mean) | 6.186" in baseline
    )
    assert "| product-colour share of the first batch | 0.208 |" in baseline


def test_the_docs_describe_the_four_fixes_and_the_measurement_change() -> None:
    adr = (ROOT / "docs" / "adr" / "0012-two-modes-shop-first.md").read_text()
    assert "Amendment, stage 2p" in adr and "clamp(floor(0.30 x batch" in adr
    assert (
        "I ran out of new results" in adr
        and "70% of a batch" in adr
        and "matched by description" in adr
    )
    assert "measurement changed after the" in adr and "category judge v2" in adr
    readme = (ROOT / "evals" / "shop" / "README.md").read_text()
    assert "The measurement changed" in readme and "CASES=case-04,case-06" in readme
    todo = (ROOT / "docs" / "todo.md").read_text()
    assert todo.count("Done (stage 2p)") == 6 and "calibration" in todo.lower()
    assert "Stage 2p" in (ROOT / "SPEC.md").read_text()
    for name in (
        "docs/sessions/2026-10-27-shop-fixes.md",
        "docs/sessions/2026-10-27-shop-fixes-progress.md",
    ):
        assert (ROOT / name).is_file(), name
