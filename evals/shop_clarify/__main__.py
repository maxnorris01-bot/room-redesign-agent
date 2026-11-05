"""CLI: `python -m evals.shop_clarify plan|mock|live|report`. See evals/shop_clarify/README.md.

`live` refuses to start unless EVAL_CLARIFY_CONFIRM=yes is in the environment (the Makefile passes it
from the command line), prints the plan first, and refuses under CI or pytest: the live run is the
owner's call and never runs from tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.config import load_env
from app.llm import complete
from app.shop.clarify_verify import MODES
from evals.shop_clarify.mock import mock_audit_caller, mock_caller, mock_verify_caller
from evals.shop_clarify.plan import plan_lines, plan_lines_round2
from evals.shop_clarify.report import newest, summarise, verdict_lines, write_report
from evals.shop_clarify.runner import (
    ROUND2_STOP_CREDITS,
    ROUND2_STOP_USD,
    Options,
    Spend,
    load_saved_questions,
    make_env,
    run_all,
    write_results,
)
from evals.shop_clarify.spec import RESULTS_DIR, load_cases

CONFIRM_VAR = "EVAL_CLARIFY_CONFIRM"


def refuse_live(environ: dict[str, str]) -> str:
    """Why a live run must not start, or "" when it may."""
    if environ.get("CI") or environ.get("PYTEST_CURRENT_TEST"):
        return "The live clarify run never runs from CI or from tests."
    if environ.get(CONFIRM_VAR) != "yes":
        return f"The live clarify run spends real money. Pass {CONFIRM_VAR}=yes to run it."
    return ""


def newest_live() -> Path | None:
    files = sorted(RESULTS_DIR.glob("clarify-live-*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.shop_clarify")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("plan", "list the requests, the expected calls and the worst-case cost; spends nothing"),
        ("mock", "the whole harness on canned candidates and canned model output (free)"),
        ("live", "live providers; needs EVAL_CLARIFY_CONFIRM=yes"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument(
            "--case", action="append", default=[], help="only these case ids (comma list)"
        )
        p.add_argument(
            "--round",
            type=int,
            choices=(1, 2),
            default=2,
            help="2 (default): verified pictures, replay-only Serper, $0.15 stop; 1: the first round",
        )
        p.add_argument(
            "--verify-mode",
            choices=MODES,
            default=os.environ.get("CLARIFY_VERIFY_MODE", "batched"),
            help="round 2: batched (up to 12 pictures a call) or single (one picture a call)",
        )
        p.add_argument(
            "--arms-from",
            type=Path,
            default=None,
            help="round 2: saved results whose questions the persona answers",
        )
        p.add_argument(
            "--skip-arms",
            action="store_true",
            help="round 2: leave out the with/without-answers pipeline runs",
        )
    rep = sub.add_parser(
        "report", help="write report.md and contact-sheet.html from a results file"
    )
    rep.add_argument("results", nargs="?", type=Path, help="default: the newest results file")
    rep.add_argument("--out", type=Path, default=None, help="write the two files here")
    args = parser.parse_args(argv)

    if args.command == "report":
        path = args.results or newest()
        if path is None or not path.is_file():
            print("No results file found; run `make clarify-mock` first.", file=sys.stderr)
            return 2
        md, sheet = write_report(path, args.out)
        payload = json.loads(path.read_text())
        print("\n".join(verdict_lines(summarise(payload), payload["mode"])))
        print(f"\nwrote {md}\nwrote {sheet}")
        return 0

    cases = load_cases()
    wanted = [x.strip() for c in args.case for x in c.split(",") if x.strip()]
    if wanted:
        unknown = sorted(set(wanted) - {c.id for c in cases})
        if unknown:
            print(f"unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in wanted]

    if args.command == "plan":
        from evals.shop.runner import eval_config

        if args.round == 2:
            print("\n".join(plan_lines_round2(eval_config("mock"), cases, args.verify_mode)))
        else:
            print("\n".join(plan_lines(eval_config("mock"), cases)))
        return 0
    if args.command == "live":
        from evals.shop.runner import eval_config

        lines = (
            plan_lines_round2(eval_config("live"), cases, args.verify_mode)
            if args.round == 2
            else plan_lines(eval_config("live"), cases)
        )
        print("\n".join(lines))
        why = refuse_live(dict(os.environ))
        if why:
            print(f"\nREFUSED: {why}", file=sys.stderr)
            return 2
        load_env()
    round2 = args.round == 2
    # Round 2 never buys a Serper call: saved responses only (a miss aborts that step, not the run).
    cache = (
        "replay"
        if round2 and args.command == "live" and not os.environ.get("EVAL_SHOP_SERPER_CACHE")
        else None
    )
    try:
        env, store = make_env(args.command, cache=cache)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    mock = args.command == "mock"
    caller = mock_caller if mock else complete
    options = None
    spend = None
    if round2:
        arms = None
        if not mock:
            source = args.arms_from or newest_live()
            if source is not None and source.is_file():
                arms = load_saved_questions(source)
                print(f"with/without-answers arms use the questions saved in {source.name}")
        options = Options(
            verify=True, mode=args.verify_mode, variants=True, arms_questions=arms,
            skip_arms=args.skip_arms,
            vision_caller=mock_verify_caller if mock else None,
            audit_caller=mock_audit_caller if mock else None,
        )  # fmt: skip
        if not mock:
            spend = Spend(limit_usd=ROUND2_STOP_USD, limit_credits=ROUND2_STOP_CREDITS)
    payload = run_all(
        cases,
        env,
        caller,
        progress=lambda m: print(m, flush=True),
        store=store,
        spend=spend,
        options=options,
    )
    path = write_results(payload)
    print(json.dumps({"stopped": payload["stopped"], "totals": payload["totals"]}, indent=2))
    print(f"results: {path}")
    failed = [c["id"] for c in payload["cases"] if c["status"] != "ok"]
    return 1 if args.command == "mock" and failed else 0


if __name__ == "__main__":
    sys.exit(main())
