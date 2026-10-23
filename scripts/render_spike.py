"""Photo-real spike: render a finished run's picks into the visitor's room photo (an experiment).

    make render                                  # mock, offline: placeholder images, full path
    make render ARGS='--from-run latest --approach gemini --locate --judge'
    make render-live ARGS='--from-run spike-output/<run id> --approach gemini --max-usd 1'

Defaults to mock. Live only with --live (use `make render-live`), only for the provider keys present
in the environment, and only after the plan and its estimated cost are printed and checked against
--max-usd. Product images go in render-inputs/<slot>.jpg (for example sofa.jpg, media-unit.jpg).
Output: render-output/<run id>/ (result.json, index.html, images). Not wired into the pipeline.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from app.config import BudgetExceededError, Config, load_env
from photoreal import inputs, specs
from photoreal.cost import CapError, plan_text
from photoreal.requests import HttpTransport, MockTransport, RenderError
from photoreal.runner import RunArgs, run_render


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--from-run", required=True, help="spike-output/<run id>, or 'latest'")
    p.add_argument("--approach", choices=specs.APPROACHES, default="gpt")
    p.add_argument("--items", help="comma separated slots to render (default: all picks)")
    p.add_argument(
        "--mode",
        choices=("all-at-once", "one-by-one"),
        help="default: all-at-once for gpt, one-by-one for gemini and decor8",
    )
    p.add_argument("--quality", choices=specs.GPT_QUALITIES, default="medium")
    p.add_argument("--mask", choices=("none", "boxed"), default="none")
    p.add_argument("--place", action="append", default=[], help='"slot=free text" (repeatable)')
    p.add_argument("--locate", action="store_true", help="one Claude vision call proposes places")
    p.add_argument("--judge", action="store_true", help="one Claude vision call scores each render")
    p.add_argument("--max-usd", type=float, default=3.00, help="hard stop on estimated spend")
    p.add_argument("--model", help="override the render model id")
    p.add_argument("--live", action="store_true", help="make real, billed calls (default: mock)")
    args = p.parse_args(argv)

    load_env()
    config = Config.from_env()
    config = replace(config, llm_mode="live" if args.live else "mock")
    run_dir = inputs.latest_run() if args.from_run == "latest" else Path(args.from_run)
    default_mode = "all-at-once" if args.approach == "gpt" else "one-by-one"
    run_args = RunArgs(
        from_run=run_dir,
        approach=args.approach,
        items=[s.strip() for s in args.items.split(",")] if args.items else None,
        mode=args.mode or default_mode,
        quality=args.quality,
        mask=args.mask,
        place=args.place,
        locate=args.locate,
        judge=args.judge,
        max_usd=args.max_usd,
        live=args.live,
        model=args.model,
    )
    keys = {k: os.environ[v] for k, v in specs.KEY_ENV.items() if os.environ.get(v)}
    transport = HttpTransport(keys) if args.live else MockTransport()
    try:
        print(f"source run: {run_dir}")
        result = run_render(
            run_args, transport, config, env=os.environ,
            on_plan=lambda plan: print(plan_text(plan, args.max_usd, live=args.live)),
        )  # fmt: skip
    except (inputs.InputError, CapError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (RenderError, BudgetExceededError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    t = result.result["totals"]
    print(
        f"render est ${t['render_estimated_usd']:.4f}, anthropic ${t['anthropic_usd']:.4f}, "
        f"running total ${t['running_total_usd']:.4f} (cap ${args.max_usd:.2f})"
    )
    if result.result.get("error"):
        print(f"STOPPED: {result.result['error']}", file=sys.stderr)
    print(f"wrote {result.out_dir}/index.html and result.json\nopen {result.out_dir}/index.html")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
