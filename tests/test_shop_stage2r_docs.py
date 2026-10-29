"""Stage 2r: the records and notes that go with the change exist and say what they must."""

from __future__ import annotations

from pathlib import Path

from evals.shop.spec import thresholds_sha256

ROOT = Path(__file__).resolve().parents[1]
HASH = "c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237"


def read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text()


def test_the_thresholds_are_untouched() -> None:
    assert thresholds_sha256() == HASH


def test_the_full_run_record_names_the_run_and_the_baseline() -> None:
    text = read("docs", "evals", "shop-eval-full-run-2026-10-29.md")
    assert "20261029-034000-3f9fa0" in text and "Nothing here replaces the baseline" in text
    assert "single run" in text and "22 checks" in text and "new failure" in text
    assert text.count("| case-") == 12


def test_the_lift_diagnosis_gives_a_verdict_for_every_case() -> None:
    text = read("docs", "evals", "shop-lift-diagnosis-2026-10-29.md")
    for n in range(1, 13):
        assert f"| case-{n:02d} |" in text
    assert "Verdicts:" in text and "changes no pipeline" in text


def test_the_adr_amendment_and_the_readme_describe_the_tools() -> None:
    adr = read("docs", "adr", "0012-two-modes-shop-first.md")
    assert "Amendment, stage 2r" in adr and "EVAL_SHOP_SERPER_CACHE" in adr
    readme = read("evals", "shop", "README.md")
    assert "EVAL_SHOP_SERPER_CACHE" in readme and "cached pool, not a fresh-world run" in readme
    assert "Measurement changes after the baseline" in readme
    assert "decidable" in read("evals", "shop", "known_instability.md")


def test_every_new_threshold_is_in_the_adr_table() -> None:
    from app.shop.thresholds import all_thresholds

    adr = read("docs", "adr", "0012-two-modes-shop-first.md")
    for name in ("FIRST_SEARCH_MAX_CALLS", "FILL_EMPTY_STREAK_MAX", "PLENTIFUL_UNSEEN",
                 "GUARD_ALT_SUPPLY_MIN", "MIN_TAG_CALL_TIMEOUT_S"):  # fmt: skip
        assert name in all_thresholds() and f"| {name} |" in adr
