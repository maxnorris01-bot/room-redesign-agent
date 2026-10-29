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
from evals.shop.spec import Case, load_cases, load_thresholds

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
    plan = sub.add_parser("plan", help="list cases, personas, thresholds hash, estimated cost")
    plan.add_argument(
        "--case", action="append", default=[], help="only these case ids (comma list)"
    )
    for name, text in (
        ("mock", "the whole harness on fixtures and deterministic judges"),
        ("live", "live providers; needs EVAL_SHOP_CONFIRM=yes"),
    ):
        run = sub.add_parser(name, help=text)
        run.add_argument(
            "--case", action="append", default=[], help="run only this case id (repeatable)"
        )
    rj = sub.add_parser(
        "rejudge",
        help="re-run only the persona judge on a results file's batches (judge noise); live files "
        "need EVAL_SHOP_CONFIRM=yes",
    )
    rj.add_argument("--results", type=Path, required=True, help="the saved results file")
    rj.add_argument("--rounds", default="1,2,3", help="comma-separated rounds (default 1,2,3)")
    rj.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    lint = sub.add_parser("lint", help="list anomalies in a saved results file (zero spend)")
    lint.add_argument("--results", type=Path, required=True)
    lift = sub.add_parser("lift", help="offline lift diagnosis of a saved results file")
    lift.add_argument("--results", type=Path, required=True)
    lift.add_argument("--out", type=Path, default=None)
    replay = sub.add_parser(
        "replay",
        help="structural check against saved Serper responses; mock model and judges, zero spend",
    )
    replay.add_argument("--case", action="append", default=[], help="only these case ids")
    rep = sub.add_parser("report", help="render a Markdown report from a results file")
    rep.add_argument("results", nargs="?", type=Path, help="default: the newest results file")
    rep.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    args = parser.parse_args(argv)

    cases = load_cases()
    thresholds = load_thresholds()
    wanted = [x.strip() for c in getattr(args, "case", []) for x in c.split(",") if x.strip()]
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
    if args.command == "rejudge":
        return rejudge_command(args, cases)
    if args.command == "lint":
        from evals.shop.lint import lint_file

        if not args.results.is_file():
            print(f"no such results file: {args.results}", file=sys.stderr)
            return 2
        print(lint_file(args.results))
        return 0
    if args.command == "lift":
        from evals.shop.diagnose_lift import render_file as lift_text

        if not args.results.is_file():
            print(f"no such results file: {args.results}", file=sys.stderr)
            return 2
        text = lift_text(args.results)
        if args.out:
            args.out.write_text(text + "\n")
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0
    if args.command == "replay":
        return replay_command(cases)
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
    try:
        env = build_env(args.command)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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


def replay_command(cases: list[Case], cache_dir: Path | None = None) -> int:
    """Zero spend: the saved Serper responses, the mock model, persona and judges."""
    from evals.shop.cache import CACHE_DIR, EMPTY_HELP, cache_entries
    from evals.shop.structural import EMPTY_MESSAGE, render

    folder = cache_dir or CACHE_DIR
    if cache_entries(folder) == 0:
        print(f"{EMPTY_MESSAGE}\n({EMPTY_HELP}; looked in {folder})", file=sys.stderr)
        return 2
    env = build_env("mock", serper_cache="replay", cache_dir=folder)
    payload = run_all(cases, env, progress=lambda m: print(m, flush=True))
    print()
    print(render(payload))
    missing = list(env.cache.missing) if env.cache is not None else []
    if missing:
        print(
            f"\n{len(missing)} request(s) were not in the saved responses; run a live eval once "
            "with recording so they are saved:",
            file=sys.stderr,
        )
        for text in dict.fromkeys(missing):
            print(f"  - {text}", file=sys.stderr)
        return 1
    return 0


def rejudge_command(args: argparse.Namespace, cases: list[Case]) -> int:
    from app.thumbnails import build_thumbnailer
    from evals.shop.judges import ModelJudge
    from evals.shop.rejudge import (
        estimate_rejudge,
        render,
        run_rejudge,
        select_batches,
        write_rejudge,
    )
    from evals.shop.runner import eval_config

    path: Path = args.results
    if not path.is_file():
        print(f"no such results file: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text())
    try:
        rounds = [int(x) for x in args.rounds.split(",") if x.strip()]
    except ValueError:
        print("--rounds must be numbers like 1,2,3", file=sys.stderr)
        return 2
    only = [x.strip() for x in args.cases.split(",") if x.strip()]
    known = {c["id"] for c in data["cases"]}
    if any(r not in (1, 2, 3) for r in rounds) or set(only) - known:
        print("unknown round or case id for this file", file=sys.stderr)
        return 2
    mode = "mock" if data["mode"] == "mock" else "live"
    config = eval_config(mode)
    batches = select_batches(data, rounds, only)
    est = estimate_rejudge(config, batches)
    print(
        f"Rejudge estimate: {est['batches']} batches, {est['items']} items, {est['judge_calls']} "
        f"persona-judge calls, about ${est['usd']:.3f} (judge calls only; no search, no pipeline)"
    )
    if mode == "live":
        why = refuse_live(dict(os.environ))
        if why:
            print(f"\nREFUSED: {why}", file=sys.stderr)
            return 2
        load_env()
    judge = ModelJudge(config, build_thumbnailer(config))
    payload = run_rejudge(data, {c.id: c for c in cases}, judge, rounds, only, source=path.name)
    out = write_rejudge(payload, RESULTS_DIR)
    print(render(payload))
    print(f"results: {out}")
    return 0


def build_env_config(mode: str) -> Config:
    from evals.shop.runner import eval_config

    return eval_config(mode)


def _newest() -> Path | None:
    files = sorted(RESULTS_DIR.glob("shop-*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


if __name__ == "__main__":
    sys.exit(main())
