"""The baseline document (first live Shop eval): it exists, is held to the committed thresholds, and
carries no links or product titles. If thresholds.json changes, this fails: the baseline is then stale
and the change must be a deliberate, separate commit."""

from __future__ import annotations

import re
from pathlib import Path

from evals.shop.spec import thresholds_sha256

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "evals" / "shop-eval-baseline-2026-10-26.md"
RECORDED_HASH = "c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237"


def test_the_baseline_is_held_to_the_thresholds_it_was_run_with() -> None:
    text = DOC.read_text()
    assert RECORDED_HASH in text
    assert thresholds_sha256() == RECORDED_HASH  # thresholds.json is exactly as in the baseline run


def test_the_baseline_has_the_report_the_diagnosis_and_the_not_changed_section() -> None:
    text = DOC.read_text()
    assert text.startswith("# Shop eval baseline")
    assert "# Shop eval report: live run 20261027-041854-b00d9f" in text
    assert "- Verdict: **fail**" in text
    rows = [
        ln
        for ln in text.split("## Cases")[0].splitlines()
        if ln.startswith("| ") and "---" not in ln
    ]
    assert len([r for r in rows if r.endswith("| pass |") or r.endswith("| FAIL |")]) == 22
    assert sum(1 for r in rows if r.endswith("| FAIL |")) == 6
    for heading in ("## Diagnosis", "### 1. Case 12", "### 2. Case 10", "### 3. Case 06", "### 4. Case 04",
                    "### 5. Lift, all 12 cases", "### 6. Judge noise", "### 7. Ranking of the findings",
                    "### Ranked fix list", "### What was deliberately not changed"):  # fmt: skip
        assert heading in text, heading
    assert (
        len([ln for ln in text.splitlines() if re.match(r"\| case-\d\d \| \d+/\d+/\d+ \|", ln)])
        == 12
    )


def test_the_baseline_carries_no_links_and_no_product_titles() -> None:
    text = DOC.read_text()
    assert not re.search(r"https?://|www\.|\.com/|thumbnail_url", text)
    assert "[FIXTURE]" not in text
    # the only seller names are canonical retailer names; no quoted listing text beyond requests
    assert "Lowe's" in text
    for line in text.splitlines():
        assert len(line) < 400 or line.startswith("|"), line[:80]


def test_the_baseline_docs_are_linked() -> None:
    assert "shop-eval-baseline-2026-10-26.md" in (ROOT / "docs" / "evals" / "README.md").read_text()
    assert "shop-eval-baseline-2026-10-26.md" in (ROOT / "evals" / "shop" / "README.md").read_text()
    assert (
        "Baseline (first live run"
        in (ROOT / "docs" / "adr" / "0012-two-modes-shop-first.md").read_text()
    )
    todo = (ROOT / "docs" / "todo.md").read_text()
    assert "eval-shop-rejudge" in todo and "Shop Again can return nothing" in todo
