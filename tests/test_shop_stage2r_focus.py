"""Stage 2r, phase 3: what "supply" means for a refine focus in the eval, the pipeline's own count
of it, and focus words kept by the fill loop's variants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.shop import scorers as sc
from evals.shop.evaluate import evaluate
from evals.shop.runner import unseen_focus_supply
from evals.shop.spec import load_thresholds

from app.shop import queries as q
from app.shop.models import ShopQuery
from app.shop.refine import RefineSet
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)
from test_shop_stage2p_notes import service


def listings(titles: list[str]) -> list[sc.Listing]:
    return [sc.Listing(f"r{i}", t, 100.0, "Shop") for i, t in enumerate(titles)]


MESH = "Ergonomic Mesh Desk Chair"
PLAIN = "Task Office Chair"


def test_case_10_round_3_is_not_scored_when_the_pool_could_not_supply_the_quota() -> None:
    """The saved re-run: 9 results shown, 1 title hit; everything unseen was shown (guarded 0,
    capped 0), so at most 1 unseen candidate carried the focus. Reserved: ceil(0.5 x 9) = 5."""
    batch = listings([MESH] + [PLAIN] * 8)
    got = sc.focus_share(batch, "mesh", ["Mesh Chair"] * 14, unseen_supply=1)
    assert got["hits"] == 1 and round(got["share"], 3) == 0.111
    assert got["reserved"] == 5 and got["supply"] is False and got["measure"] == "unseen"


def test_supply_exists_when_the_unseen_candidates_cover_the_quota() -> None:
    batch = listings([MESH] + [PLAIN] * 8)
    assert sc.focus_share(batch, "mesh", unseen_supply=5)["supply"] is True
    assert sc.focus_share(batch, "mesh", unseen_supply=4)["supply"] is False
    assert sc.focus_share([], "mesh", unseen_supply=9)["supply"] is False


def test_the_old_reading_is_kept_for_results_without_the_pipelines_count() -> None:
    """Pinned: with no `unseen_supply` supply is a title among everything already shown, or in the
    batch (the 2p definition, which counts items that cannot be shown again)."""
    batch = listings([MESH] + [PLAIN] * 8)
    old = sc.focus_share(batch, "mesh", ["Mesh Chair"] * 14)
    assert (
        old["supply"] is True and old["measure"] == "seen titles" and old["supply_unseen"] is None
    )
    assert sc.focus_share(listings([PLAIN] * 3), "mesh", [])["supply"] is False


def row(share: float, supply: bool) -> dict[str, Any]:
    return {"round": 2, "price": {"share": 1.0}, "seconds": 1.0, "refine": {
        "focus": {"hits": 1, "share": share, "supply": supply}}}  # fmt: skip


def test_the_check_reads_only_rounds_with_supply_and_the_threshold_is_unchanged() -> None:
    t = load_thresholds()
    assert t.refine_focus_share_min == 0.5
    rounds = [row(0.609, True), row(0.111, False)]
    case: dict[str, Any] = {"id": "case-10", "status": "ok", "rounds": rounds, "lift": {},
                            "cost": {"pipeline_usd": 0.02}}  # fmt: skip
    checks = {c["key"]: c for c in evaluate([case], t)}
    assert (
        checks["refine_focus_share_min"]["value"] == 0.609
        and checks["refine_focus_share_min"]["passed"]
    )
    case["rounds"][1] = row(0.111, True)  # the 2p reading of the same round
    failing = {c["key"]: c for c in evaluate([case], t)}["refine_focus_share_min"]
    assert failing["value"] == 0.111 and failing["passed"] is False


def test_the_runner_reads_the_pipelines_count_from_the_trace() -> None:
    view = {"search": {"trace": {"batch": {"focus_supply": {"unseen_pool": 9, "pairs": [
        {"dimension": "note", "value": "x", "unseen_flagged": 3, "in_batch": 1}]}}}}}  # fmt: skip
    assert unseen_focus_supply(view) == 3
    assert unseen_focus_supply({"search": {"trace": {}}}) is None


def test_a_shop_again_reports_how_many_unseen_candidates_match_the_focus(tmp_path: Path) -> None:
    svc, sid, first = service(tmp_path)
    svc.refine(sid, "focus on fluted")
    again = svc.shop_again(sid)["search"]
    info = again["trace"]["batch"]["focus_supply"]
    assert info["unseen_pool"] > 0 and info["pairs"][0]["dimension"] == "note"
    assert info["pairs"][0]["unseen_flagged"] >= info["pairs"][0]["in_batch"] > 0


# ---- the variants keep the focus words -----------------------------------------------------------


def desk() -> q.QueryInput:
    base = ShopQuery(
        request="ergonomic desk chairs under $300", search_text="ergonomic desk chair",
        category="desk chair", category_key="desk chair", must_have=["ergonomic"],
    )  # fmt: skip
    return q.QueryInput(base=base, adjusted=base, focus_words=["mesh backs"])


def test_a_variant_without_the_focus_words_is_rejected() -> None:
    assert "typed focus 'mesh backs'" in q.validate_query("premium ergonomic desk chair", desk())[1]
    assert q.validate_query("ergonomic mesh back desk chair", desk())[1] == ""
    assert q.validate_query("popular ergonomic desk chair mesh backs", desk())[1] == ""


def test_fallback_variants_carry_the_focus_words() -> None:
    inp = desk()
    inp.used = ["ergonomic desk chair"]
    got = q.fallback_variants(inp)
    assert got and all("mesh" in t and "back" in t for t in got), got


def test_the_refine_set_lists_the_words_of_its_focus_chips() -> None:
    rs = RefineSet()
    rs.focus_notes = {"n1": "mesh backs"}
    rs.focus_values = {("color", "navy_dark_blue"): "c1", ("material", "wood"): "c2"}
    assert rs.focus_words() == ["mesh backs", "navy", "wood"]
    assert RefineSet().focus_words() == []
