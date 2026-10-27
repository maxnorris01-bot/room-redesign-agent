"""CLI: `python -m evals.shop plan|mock|live|report`. See evals/shop/README.md.

`live` refuses to start unless EVAL_SHOP_CONFIRM=yes is in the environment (the Makefile passes it
from the command line), prints the estimate first, and refuses under CI or pytest: the live eval is
the owner's call and never runs from tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.config import Config, load_env
from evals.shop.plan import plan_lines
from evals.shop.report import render_file
from evals.shop.runner import RESULTS_DIR, build_env, run_all, write_results
from evals.shop.spec import load_cases, load_thresholds

CONFIRM_VAR = "EVAL_SHOP_CONFIRM"


def refuse_live(environ: dict[str, str]) -> str:
    """Why a live run must not start, or "" when it may."""
    if environ.get("CI") or environ.get("PYTEST_CURRENT_TEST"):
        return "The live Shop eval never runs from CI or from tests."
    if environ.get(CONFIRM_VAR) != "yes":
        return f"The live Shop eval spends real money. Pass {CONFIRM_VAR}=yes to run it."
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.shop")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="list cases, personas, thresholds hash, estimated cost")
    for name, text in (
        ("mock", "the whole harness on fixtures and deterministic judges"),
        ("live", "live providers; needs EVAL_SHOP_CONFIRM=yes"),
    ):
        run = sub.add_parser(name, help=text)
        run.add_argument(
            "--case", action="append", default=[], help="run only this case id (repeatable)"
        )
    rep = sub.add_parser("report", help="render a Markdown report from a results file")
    rep.add_argument("results", nargs="?", type=Path, help="default: the newest results file")
    rep.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    args = parser.parse_args(argv)

    cases = load_cases()
    thresholds = load_thresholds()
    wanted = getattr(args, "case", [])
    if wanted:
        unknown = sorted(set(wanted) - {c.id for c in cases})
        if unknown:
            print(f"unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in wanted]
    if args.command == "plan":
        from evals.shop.runner import eval_config

        print("\n".join(plan_lines(eval_config("mock"), cases, thresholds)))
        return 0
    if args.command == "report":
        path = args.results or _newest()
        if path is None:
            print("No results file found; run `make eval-shop-mock` first.", file=sys.stderr)
            return 2
        text = render_file(path)
        if args.out:
            args.out.write_text(text)
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0
    if args.command == "live":
        why = refuse_live(dict(os.environ))
        print("\n".join(plan_lines(build_env_config("live"), cases, thresholds)))
        if why:
            print(f"\nREFUSED: {why}", file=sys.stderr)
            return 2
        load_env()
    env = build_env(args.command)
    payload = run_all(cases, env, progress=lambda m: print(m, flush=True))
    path = write_results(payload)
    print(json.dumps({"verdict": payload["verdict"], "totals": payload["totals"]}, indent=2))
    print(f"results: {path}")
    if args.command == "mock":
        return (
            0
            if payload["verdict"]["status"] != "incomplete"
            and not payload["verdict"]["cases_not_ok"]
            else 1
        )
    return 0 if payload["verdict"]["status"] == "pass" else 1


def build_env_config(mode: str) -> Config:
    from evals.shop.runner import eval_config

    return eval_config(mode)


def _newest() -> Path | None:
    files = sorted(RESULTS_DIR.glob("shop-*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


if __name__ == "__main__":
    sys.exit(main())
