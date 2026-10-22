"""Cost-and-latency spike: photo in, perception + retrieval out, per-stage table printed.

    python scripts/spike.py spike-photos/living-room.jpg --budget 800 --style "warm minimalist"
    python scripts/spike.py spike-photos/living-room.jpg --perception-only --perception-effort low

Defaults to mock (APP_LLM_MODE=mock): no network, no API key. The same script runs live with only
an env change (`make spike-live ARGS=...`, Max's call). Output goes to the gitignored
spike-output/<run_id>/ (result.json, table.txt, trace.jsonl). Photos belong in spike-photos/.
Pass `--synthetic` instead of a photo path to use a generated placeholder image.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.config import BudgetExceededError, Config, load_env
from app.images import synthetic_room_image
from app.llm import LLMOutputError
from app.schemas import SchemaError
from app.shopping import ProviderError
from app.spike import format_table, point_trace_at, run_spike, write_outputs

OUT_ROOT = Path("spike-output")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "photo", nargs="?", type=Path, help="path to a room photo (keep it in spike-photos/)"
    )
    parser.add_argument(
        "--synthetic", action="store_true", help="use a generated placeholder image"
    )
    parser.add_argument("--budget", type=float, help="total furniture budget, USD")
    parser.add_argument("--style", help='style goal, e.g. "warm minimalist"')
    parser.add_argument(
        "--slots", type=int, default=None, help="furniture slots to search (default: config)"
    )
    parser.add_argument(
        "--perception-only", action="store_true", help="skip retrieval; time perception alone"
    )
    parser.add_argument("--perception-model", help="override APP_MODEL_PERCEPTION for this run")
    parser.add_argument(
        "--perception-effort", help="override APP_EFFORT_PERCEPTION (low|medium|high|xhigh|max)"
    )
    parser.add_argument(
        "--image-long-edge", type=int, help="override APP_IMAGE_MAX_EDGE (default 1568)"
    )
    parser.add_argument(
        "--perception-prompt-version",
        default=None,
        help="perception prompt version to load (default: config, currently 2; 1 is kept)",
    )
    parser.add_argument(
        "--retrieval-mode",
        choices=("shopping", "web_search"),
        help="override APP_RETRIEVAL_MODE (default shopping)",
    )
    parser.add_argument(
        "--search-provider", choices=("serper", "serpapi"), help="override APP_SEARCH_PROVIDER"
    )
    parser.add_argument(
        "--finalists", type=int, help="finalists per slot (override APP_FINALISTS_PER_SLOT)"
    )
    args = parser.parse_args(argv)
    if not args.perception_only and (args.budget is None or args.style is None):
        parser.error("--budget and --style are required unless --perception-only is given")
    if (args.photo is None) == (not args.synthetic):
        parser.error("give exactly one of: a photo path, or --synthetic")
    if args.photo is not None and not args.photo.is_file():
        parser.error(f"photo not found: {args.photo}")

    load_env()
    config = Config.from_env()
    overrides = {
        "model_perception": args.perception_model,
        "effort_perception": args.perception_effort,
        "image_max_edge": args.image_long_edge,
        "perception_prompt_version": args.perception_prompt_version,
        "retrieval_mode": args.retrieval_mode,
        "search_provider": args.search_provider,
        "finalists_per_slot": args.finalists,
    }
    config = replace(config, **{k: v for k, v in overrides.items() if v is not None})
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    trace_path = point_trace_at(OUT_ROOT, run_id)
    photo = synthetic_room_image() if args.synthetic else args.photo
    try:
        result = run_spike(
            photo,
            budget_usd=args.budget or 0.0,
            style=args.style or "",
            perception_only=args.perception_only,
            slots=args.slots,
            config=config,
            run_id=run_id,
        )
    except BudgetExceededError as exc:
        print(
            f"STOPPED: budget cap hit ({exc}). Nothing was truncated; raise the cap deliberately.",
            file=sys.stderr,
        )
        return 2
    except ProviderError as exc:  # message names the provider and HTTP status, never a key or URL
        print(f"FAILED: shopping provider error: {exc}", file=sys.stderr)
        return 4
    except (SchemaError, LLMOutputError) as exc:
        print(f"FAILED: unusable model output: {exc}", file=sys.stderr)
        return 3
    out_dir = write_outputs(result, OUT_ROOT)
    print(format_table(result))
    print(f"\nwrote {out_dir}/result.json, table.txt, and {trace_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
