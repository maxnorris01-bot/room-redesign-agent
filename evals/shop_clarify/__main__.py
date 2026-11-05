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
from evals.shop_clarify.mock import mock_caller
from evals.shop_clarify.plan import plan_lines
from evals.shop_clarify.report import newest, summarise, verdict_lines, write_report
from evals.shop_clarify.runner import make_env, run_all, write_results
from evals.shop_clarify.spec import load_cases

CONFIRM_VAR = "EVAL_CLARIFY_CONFIRM"


def refuse_live(environ: dict[str, str]) -> str:
    """Why a live run must not start, or "" when it may."""
    if environ.get("CI") or environ.get("PYTEST_CURRENT_TEST"):
        return "The live clarify run never runs from CI or from tests."
    if environ.get(CONFIRM_VAR) != "yes":
        return f"The live clarify run spends real money. Pass {CONFIRM_VAR}=yes to run it."
    return ""


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

        print("\n".join(plan_lines(eval_config("mock"), cases)))
        return 0
    if args.command == "live":
        from evals.shop.runner import eval_config

        print("\n".join(plan_lines(eval_config("live"), cases)))
        why = refuse_live(dict(os.environ))
        if why:
            print(f"\nREFUSED: {why}", file=sys.stderr)
            return 2
        load_env()
    try:
        env, store = make_env(args.command)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    caller = mock_caller if args.command == "mock" else complete
    payload = run_all(cases, env, caller, progress=lambda m: print(m, flush=True), store=store)
    path = write_results(payload)
    print(json.dumps({"stopped": payload["stopped"], "totals": payload["totals"]}, indent=2))
    print(f"results: {path}")
    failed = [c["id"] for c in payload["cases"] if c["status"] != "ok"]
    return 1 if args.command == "mock" and failed else 0


if __name__ == "__main__":
    sys.exit(main())
