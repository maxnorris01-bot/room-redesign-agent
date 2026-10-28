"""Shop eval, phase 2: the code scorers, threshold evaluation, and the model judges (schema checks,
failure handling, isolation). Offline: the judges run on the mock client or a fake caller."""

from __future__ import annotations

import json
from typing import Any

import pytest
from evals.shop import scorers as sc
from evals.shop.evaluate import evaluate, verdict
from evals.shop.judges import (
    BATCH,
    CATEGORY_TOOL,
    PERSONA_TOOL,
    CategoryJudgements,
    JudgeItem,
    ModelJudge,
    PersonaJudgements,
    check_indexes,
    clean_title,
)
from evals.shop.spec import load_cases, load_thresholds

from app.config import Config
from app.llm import Budget, Completion, LLMOutputError
from app.thumbnails import MockThumbnailer

T = load_thresholds()
CASES = {c.id: c for c in load_cases()}


def L(
    n: int, title: str = "Walnut Sofa", price: float | None = 100.0, seller: str = "A"
) -> sc.Listing:
    return sc.Listing(id=f"r{n}", title=title, price=price, seller=seller)


# ---- code scorers ----------------------------------------------------------------------------


def test_price_compliance_counts_results_inside_the_parsed_range() -> None:
    items = [L(1, price=50), L(2, price=150), L(3, price=100), L(4, price=None)]
    got = sc.price_compliance(items, None, 100.0)
    assert got["within"] == 2 and got["unpriced"] == 1 and got["share"] == 0.5
    assert sc.price_compliance(items[:1], None, 100.0)["share"] == 1.0
    assert sc.price_compliance(items, None, None)["share"] == 1.0  # no range: everything complies
    assert sc.price_compliance([L(1, price=20)], 30.0, 40.0)["share"] == 0.0


def test_repeats_are_items_shown_in_two_rounds() -> None:
    b1 = sc.Batch(1, "search", [L(1), L(2)])
    b2 = sc.Batch(2, "shop_again", [L(3), L(4)])
    assert sc.repeats([b1, b2]) == {"count": 0, "ids": []}
    b3 = sc.Batch(3, "shop_again", [L(2), L(5)])
    assert sc.repeats([b1, b2, b3]) == {"count": 1, "ids": ["r2"]}
    twice = sc.Batch(
        1, "search", [L(1), L(1)]
    )  # the same round twice is not a repeat across rounds
    assert sc.repeats([twice])["count"] == 0


def test_seller_diversity() -> None:
    items = [L(n, seller=s) for n, s in enumerate(["West Elm", "West Elm", "IKEA", "Target"], 1)]
    got = sc.seller_diversity(items)
    assert got["distinct"] == 3 and got["top"] == 2 and got["top_share"] == 0.5
    assert sc.seller_diversity([])["distinct"] == 0
    same = [L(1, seller="westelm.com"), L(2, seller="West Elm")]  # one seller, canonicalized
    assert sc.seller_diversity(same)["distinct"] == 1


def test_near_duplicates_use_the_pipeline_signals() -> None:
    a = L(1, "Brushed Brass Round Coffee Table with Shelf", 200.0, "Etsy")
    b = L(2, "Brushed Brass Round Coffee Table with Shelf by Overstock", 210.0, "Overstock")
    c = L(3, "Glass Oval Side Table", 90.0, "IKEA")
    got = sc.near_duplicate_groups([a, b, c])
    assert got["groups"] == 1 and got["sizes"] == [2]
    assert sc.near_duplicate_groups([a, c])["groups"] == 0
    far = L(4, a.title, 900.0, "Wayfair")  # the same words at a very different price: not one
    assert sc.near_duplicate_groups([a, far])["groups"] == 0
    f1 = sc.Listing("x", "Chair A", 50.0, "A", fingerprint=0b1010)
    f2 = sc.Listing("y", "Other words entirely", 52.0, "B", fingerprint=0b1010)
    assert sc.near_duplicate_groups([f1, f2])["groups"] == 1  # the image signal alone


def test_context_leakage_finds_whole_words_only() -> None:
    texts = ["boho curtains", "boho curtains navy"]
    got = sc.context_leakage(texts, ["navy", "dark blue", "blue"])
    assert got == {"count": 1, "words": ["navy"]}
    assert sc.context_leakage(["boho curtains"], ["navy", "blue"])["count"] == 0
    assert sc.context_leakage(["bluebird curtains"], ["blue"])["count"] == 0  # not a whole word
    assert sc.context_leakage(["dark blue curtains"], ["dark blue"])["count"] == 1


def test_colour_shares_use_the_title_vocabulary() -> None:
    items = [L(1, "Navy Velvet Armchair"), L(2, "Cream Boucle Armchair"), L(3, "Dark Blue Chair")]
    assert sc.product_color_share(items, "navy_dark_blue")["count"] == 2
    assert sc.avoid_color_share(items, ["navy_dark_blue", "blue"])["share"] == pytest.approx(2 / 3)
    assert sc.avoid_color_share([], ["blue"])["share"] == 0.0


def test_refine_exclusion_and_focus() -> None:
    items = [L(1, seller="Walmart"), L(2, seller="walmart.com"), L(3, seller="IKEA")]
    ex = sc.exclusion_respected(items, ["Walmart"])
    assert ex["violations"] == 2 and ex["share"] == pytest.approx(1 / 3)
    assert sc.exclusion_respected(items[2:], ["Walmart"])["share"] == 1.0
    chairs = [L(1, "Mesh Back Chair"), L(2, "Leather Chair"), L(3, "MESH office chair")]
    got = sc.focus_share(chairs, "mesh")
    assert got["hits"] == 2 and got["supply"] is True and got["share"] == pytest.approx(2 / 3)
    none = sc.focus_share(chairs[1:2], "mesh", supply_titles=["Mesh Chair elsewhere"])
    assert none["hits"] == 0 and none["supply"] is True  # supply existed and was not used
    assert sc.focus_share(chairs[1:2], "mesh")["supply"] is False


def test_rates_leave_failed_judgements_out_and_count_them() -> None:
    got = sc.rate(["yes", "no", None, "yes"], {"yes"})
    assert got == {"scored": 3, "failed": 1, "good": 2, "rate": pytest.approx(2 / 3)}
    assert sc.rate([None, None], {"yes"})["rate"] is None
    assert sc.median([3.0, 1.0, 2.0]) == 2.0 and sc.median([]) is None


# ---- threshold evaluation --------------------------------------------------------------------


def good_round(n: int = 1, seconds: float = 10.0) -> dict[str, Any]:
    return {
        "round": n, "kind": "search" if n == 1 else "shop_again", "seconds": seconds,
        "price": {"share": 1.0}, "category": {"rate": 0.95}, "attributes": {"rate": 0.8},
        "sellers": {"distinct": 8, "top_share": 0.2}, "near_dups": {"groups": 0},
        "avoid": None, "product_color": None, "refine": None,
    }  # fmt: skip


def good_case(cid: str = "case-01", lift: float = 20.0) -> dict[str, Any]:
    return {
        "id": cid, "status": "ok", "error": None, "kind": "plain",
        "rounds": [good_round(1, 12.0), good_round(2, 8.0), good_round(3, 9.0)],
        "context": {}, "repeats": {"count": 0, "ids": []},
        "lift": {"r2_minus_r1": lift, "r3_minus_r1": lift + 5},
        "cost": {"pipeline_usd": 0.02, "judge_usd": 0.05, "serper_credits": 14},
    }  # fmt: skip


def by_key(checks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["key"]: c for c in checks}


def test_a_clean_run_passes_every_check_that_has_data() -> None:
    results = [good_case(f"case-{n:02d}") for n in range(1, 13)]
    checks = evaluate(results, T)
    assert all(c["passed"] in (True, None) for c in checks)
    keys = by_key(checks)
    assert keys["avoid_color_share_max"]["passed"] is None  # no context case in this run
    assert verdict(results, checks)["status"] == "pass"
    assert {c["key"] for c in checks} >= {
        "price_compliance_min", "category_mean_min", "lift_mean_min_points", "again_max_s_max",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("mutate", "key"),
    [
        (lambda r: r["rounds"][1]["price"].update(share=0.96), "price_compliance_min"),
        (lambda r: r["rounds"][0]["category"].update(rate=0.70), "category_case_min"),
        (lambda r: r["rounds"][0]["attributes"].update(rate=0.40), "attribute_case_min"),
        (lambda r: r["rounds"][0]["sellers"].update(distinct=5), "distinct_sellers_min"),
        (lambda r: r["rounds"][0]["sellers"].update(top_share=0.35), "top_seller_share_max"),
        (lambda r: r["rounds"][0]["near_dups"].update(groups=2), "near_duplicates_case_max"),
        (lambda r: r["repeats"].update(count=1), "repeats_max"),
        (lambda r: r["rounds"][1].update(seconds=46.0), "again_max_s_max"),
        (lambda r: r["cost"].update(pipeline_usd=0.06), "cost_per_request_usd_max"),
        (lambda r: r["lift"].update(r2_minus_r1=-11.0), "lift_case_worst_min_points"),
    ],
)
def test_each_threshold_can_fail_on_one_bad_case(mutate: Any, key: str) -> None:
    results = [good_case(f"case-{n:02d}") for n in range(1, 13)]
    mutate(results[0])
    checks = by_key(evaluate(results, T))
    assert checks[key]["passed"] is False, checks[key]
    assert verdict(results, list(checks.values()))["status"] == "fail"


def test_the_latency_medians_need_most_cases_to_be_slow() -> None:
    results = [good_case(f"case-{n:02d}") for n in range(1, 13)]
    results[0]["rounds"][0]["seconds"] = 90.0  # one slow search does not move a median
    assert by_key(evaluate(results, T))["search_median_s_max"]["passed"] is True
    for r in results[:7]:
        r["rounds"][0]["seconds"] = 31.0
        r["rounds"][2]["seconds"] = 21.0
        r["rounds"][1]["seconds"] = 21.0
    keys = by_key(evaluate(results, T))
    assert keys["search_median_s_max"]["passed"] is False
    assert keys["again_median_s_max"]["passed"] is False


def test_means_fail_when_the_average_is_low_even_if_no_case_is_terrible() -> None:
    results = [good_case(f"case-{n:02d}") for n in range(1, 13)]
    for r in results[:6]:
        r["rounds"][0]["category"]["rate"] = 0.80
    keys = by_key(evaluate(results, T))
    assert keys["category_mean_min"]["value"] == pytest.approx(0.875)
    assert keys["category_mean_min"]["passed"] is False and keys["category_case_min"]["passed"]


def test_lift_checks() -> None:
    results = [good_case(f"case-{n:02d}", lift=5.0) for n in range(1, 13)]
    keys = by_key(evaluate(results, T))
    assert keys["lift_mean_min_points"]["passed"] is False  # +5 is under +15
    assert (
        keys["lift_improved_cases_min"]["value"] == 12 and keys["lift_improved_cases_min"]["passed"]
    )
    results = [good_case(f"case-{n:02d}", lift=40.0 if n <= 7 else -2.0) for n in range(1, 13)]
    keys = by_key(evaluate(results, T))
    assert (
        keys["lift_improved_cases_min"]["value"] == 7
        and not keys["lift_improved_cases_min"]["passed"]
    )
    assert (
        keys["lift_mean_min_points"]["passed"] is True
    )  # mean carried by seven cases: gate catches it


def test_context_colour_and_refine_checks() -> None:
    results = [good_case()]
    results[0]["rounds"][0]["avoid"] = {"share": 0.2}
    results[0]["rounds"][0]["product_color"] = {"share": 0.6}
    results[0]["context"] = {"leakage": {"count": 1, "words": ["navy"]}}
    results[0]["rounds"][1]["refine"] = {
        "exclusion": {"share": 0.95}, "focus": {"share": 0.3, "supply": True},
    }  # fmt: skip
    keys = by_key(evaluate(results, T))
    for key in ("avoid_color_share_max", "product_color_share_min", "refine_exclusion_min",
                "context_colors_in_search_text_max", "refine_focus_share_min"):  # fmt: skip
        assert keys[key]["passed"] is False, key
    results[0]["rounds"][1]["refine"]["focus"]["supply"] = False  # no supply: not scored
    assert by_key(evaluate(results, T))["refine_focus_share_min"]["passed"] is None


def test_an_errored_case_fails_the_run_and_a_stopped_run_is_incomplete() -> None:
    results = [
        good_case("case-01"),
        {"id": "case-02", "status": "failed", "error": "boom", "rounds": []},
    ]
    checks = evaluate(results, T)
    v = verdict(results, checks)
    assert v["status"] == "fail" and v["cases_not_ok"] == ["case-02"]
    assert verdict(results, checks, stopped="spend")["status"] == "incomplete"


# ---- judges ----------------------------------------------------------------------------------


def items(n: int) -> list[JudgeItem]:
    return [JudgeItem(i, f"Walnut Sofa {i}", 100.0 + i, "IKEA", "") for i in range(1, n + 1)]


def mock_judge() -> ModelJudge:
    return ModelJudge(Config(llm_mode="mock", tracing_disabled=True), MockThumbnailer())


def test_the_mock_judges_answer_every_item_deterministically() -> None:
    judge = mock_judge()
    case = CASES["case-01"]
    rows = [
        JudgeItem(
            1, "Cream Boucle Curved Sofa", 500.0, "West Elm", "https://mock.example/thumb/1.jpg"
        ),
        JudgeItem(2, "Replacement Sofa Cover Slipcover", 30.0, "Etsy", ""),
        JudgeItem(3, "Black Leather Rolled Arm Sofa", 700.0, "Wayfair", ""),
        JudgeItem(4, "Garden Spade", 10.0, "Walmart", ""),
    ]
    cat = judge.category(case, rows)
    assert cat[1] is not None and cat[1].category == "yes"
    assert cat[2] is not None and cat[2].category == "accessory_or_part"
    assert cat[4] is not None and cat[4].category == "no"
    per = judge.persona(case, rows)
    assert per[1] is not None and per[1].answer in ("love", "like")
    assert per[3] is not None and per[3].answer == "dislike"  # leather and rolled arms
    again = mock_judge().persona(case, rows)
    assert {k: v.answer for k, v in per.items() if v} == {
        k: v.answer for k, v in again.items() if v
    }
    assert judge.stats.failed_items == 0 and judge.stats.images_used >= 1


def test_calls_carry_at_most_twelve_items() -> None:
    sizes: list[int] = []

    def caller(**kw: Any) -> Completion:
        rows = [ln for ln in kw["user_text"].splitlines() if ln.split(" | ")[0].isdigit()]
        sizes.append(len(rows))
        return Completion(
            text="",
            tool_input={
                "items": [{"index": int(r.split(" | ")[0]), "answer": "like"} for r in rows]
            },
        )

    judge = ModelJudge(Config(llm_mode="mock", tracing_disabled=True), None, caller=caller)
    got = judge.persona(CASES["case-01"], items(30))
    assert sizes == [12, 12, 6] and BATCH == 12
    assert len(got) == 30 and all(v is not None for v in got.values())


def bad_then_good(first: dict[str, Any] | Exception, name: str = "answer") -> Any:
    calls: list[int] = []

    def caller(**kw: Any) -> Completion:
        calls.append(1)
        if len(calls) == 1:
            if isinstance(first, Exception):
                raise first
            return Completion(text="", tool_input=first)
        rows = [ln for ln in kw["user_text"].splitlines() if ln.split(" | ")[0].isdigit()]
        return Completion(
            text="",
            tool_input={"items": [{"index": int(r.split(" | ")[0]), name: "like"} for r in rows]},
        )

    return caller, calls


@pytest.mark.parametrize(
    "first",
    [
        {"items": [{"index": 1, "answer": "maybe"}]},  # a value outside the fixed list
        {"items": [{"index": 1, "answer": "like"}]},  # an index missing
        {
            "items": [
                {"index": 1, "answer": "like"},
                {"index": 1, "answer": "like"},
                {"index": 2, "answer": "like"},
            ]
        },
        {
            "items": [
                {"index": 1, "answer": "like"},
                {"index": 2, "answer": "like"},
                {"index": 9, "answer": "like"},
            ]
        },
        {"items": "nothing"},
        {},
        LLMOutputError("truncated"),
    ],
)
def test_a_bad_judge_answer_is_retried_once_then_accepted(first: Any) -> None:
    caller, calls = bad_then_good(first)
    judge = ModelJudge(Config(llm_mode="mock", tracing_disabled=True), None, caller=caller)
    got = judge.persona(CASES["case-01"], items(2))
    assert len(calls) == 2 and judge.stats.retries == 1 and judge.stats.failed_items == 0
    assert all(v is not None and v.answer == "like" for v in got.values())


def test_two_bad_answers_are_recorded_as_judge_failed_never_scored() -> None:
    def caller(**kw: Any) -> Completion:
        return Completion(text="", tool_input={"items": [{"index": 1, "answer": "maybe"}]})

    judge = ModelJudge(Config(llm_mode="mock", tracing_disabled=True), None, caller=caller)
    got = judge.persona(CASES["case-01"], items(3))
    assert got == {1: None, 2: None, 3: None}
    assert judge.stats.failed_items == 3 and judge.stats.calls == 2
    rated = sc.rate([v.answer if v else None for v in got.values()], {"love", "like"})
    assert rated["rate"] is None and rated["failed"] == 3  # not a zero, not a pass


def test_a_budget_error_is_not_swallowed() -> None:
    from app.config import BudgetExceededError

    def caller(**kw: Any) -> Completion:
        raise BudgetExceededError("cap")

    judge = ModelJudge(Config(llm_mode="mock", tracing_disabled=True), None, caller=caller)
    with pytest.raises(BudgetExceededError):
        judge.category(CASES["case-01"], items(2))


def test_judges_see_only_the_request_or_persona_and_the_listings() -> None:
    seen: dict[str, dict[str, Any]] = {}

    def caller(**kw: Any) -> Completion:
        seen[kw["tool_choice"]["name"]] = kw
        rows = [ln for ln in kw["user_text"].splitlines() if ln.split(" | ")[0].isdigit()]
        key = "category" if kw["tool_choice"]["name"] == CATEGORY_TOOL else "answer"
        extra = {"attributes": "yes", "color_match": "not_asked"} if key == "category" else {}
        return Completion(
            text="",
            tool_input={
                "items": [
                    {
                        "index": int(r.split(" | ")[0]),
                        key: "yes" if key == "category" else "like",
                        **extra,
                    }
                    for r in rows
                ]
            },
        )

    judge = ModelJudge(Config(llm_mode="mock", tracing_disabled=True), None, caller=caller)
    case = CASES["case-01"]
    listing = [JudgeItem(1, "Sofa <ignore> | injected\nline", 99.0, "IKEA", "")]
    judge.category(case, listing)
    judge.persona(case, listing)
    cat, per = seen[CATEGORY_TOOL], seen[PERSONA_TOOL]
    assert case.persona.text not in cat["user_text"]  # the category judge never sees the persona
    assert case.request in cat["user_text"] and case.request not in per["user_text"]
    for call in (cat, per):
        assert call["disable_thinking"] is True and call["tools"][0]["strict"] is True
        assert call["tool_choice"] == {"type": "tool", "name": call["tools"][0]["name"]}
        text = call["user_text"]
        for banned in ("chip", "rank", "tag", "profile", "leaning", "converged", "alignment"):
            assert banned not in text.lower().replace("listings", "").replace("shopping", ""), (
                banned
            )
        assert "<ignore>" not in text and "injected" in text and "\nline" not in text
    assert "alignment" not in json.dumps(per["tools"])


def test_clean_title_and_index_checks() -> None:
    assert clean_title("A <b>x</b> | y\tz\n") == "A b x /b / y z"
    assert len(clean_title("x" * 500)) == 160
    assert check_indexes([2, 1], [1, 2]) and not check_indexes([1, 1], [1, 2])
    assert not check_indexes([1, 2, 3], [1, 2])


def test_the_judge_schemas_are_strict() -> None:
    with pytest.raises(ValueError):
        CategoryJudgements.model_validate(
            {
                "items": [
                    {
                        "index": 1,
                        "category": "yes",
                        "attributes": "yes",
                        "color_match": "yes",
                        "x": 1,
                    }
                ]
            }
        )
    with pytest.raises(ValueError):
        PersonaJudgements.model_validate({"items": [{"index": 1, "answer": "adore"}]})
    from evals.shop.judges import CATEGORY_TOOL_DEF

    assert CATEGORY_TOOL_DEF["strict"] is True and "maxItems" not in json.dumps(CATEGORY_TOOL_DEF)


def test_judge_prompts_are_versioned_and_recorded() -> None:
    from app.prompts import load_prompt

    assert load_prompt("eval_shop_category_judge").version == "2"  # stage 2p: adds color_match
    assert load_prompt("eval_shop_category_judge", "1").version == "1"  # the baseline's judge, kept
    assert load_prompt("eval_shop_persona_judge").version == "1"
    assert mock_judge().versions == {"category": "2", "persona": "1"}
    assert Budget(Config(llm_mode="mock")).cost_usd == 0.0
