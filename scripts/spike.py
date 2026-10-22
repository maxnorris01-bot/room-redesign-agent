"""Cost-and-latency spike: photo in, perception + retrieval out, per-stage table printed.

    python scripts/spike.py spike-photos/living-room.jpg --style "warm minimalist"
    python scripts/spike.py spike-photos/living-room.jpg --style "warm minimalist" --items 7
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
from app.preferences import PreferenceError, check_flag_overlap, split_names
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
    parser.add_argument(
        "--budget",
        type=float,
        help="total furniture budget, USD (omit for freeplay: no budget, a price tier instead)",
    )
    parser.add_argument(
        "--price-tier",
        choices=("value", "mid", "premium"),
        help="freeplay only: value, mid (default) or premium; an error together with --budget",
    )
    parser.add_argument(
        "--items",
        type=int,
        help="pieces to shop for, 2 to APP_MAX_ITEMS (default 5 freeplay, 3 budget)",
    )
    parser.add_argument(
        "--lookup-alternates",
        action="store_true",
        help="also look up the unverified alternates now (testing; costs one lookup each)",
    )
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
    parser.add_argument(
        "--no-proposal",
        action="store_true",
        help="stop after retrieval (no proposal, self-check or revision)",
    )
    parser.add_argument(
        "--revisions", type=int, help="automatic revisions after a failed check (0 disables)"
    )
    parser.add_argument("--proposal-model", help="override APP_MODEL_PROPOSAL for this run")
    parser.add_argument("--proposal-effort", help="override APP_EFFORT_PROPOSAL for this run")
    parser.add_argument(
        "--replace",
        help="existing furniture to replace: comma separated ids (f1) or categories (sofa)",
    )
    parser.add_argument(
        "--keep", help="existing furniture to keep: comma separated ids (f1) or categories"
    )
    parser.add_argument(
        "--proposal-prompt-version",
        help="proposal prompt version to load (default: config, currently 2; 1 is kept)",
    )
    parser.add_argument(
        "--rank-prompt-version",
        help="rank prompt version to load (default: config, currently 3; 2 is kept)",
    )
    args = parser.parse_args(argv)
    if not args.perception_only and args.style is None:
        parser.error("--style is required unless --perception-only is given")
    if args.price_tier is not None and args.budget is not None:
        parser.error("--price-tier only applies to freeplay: drop --budget, or drop --price-tier")
    if (args.photo is None) == (not args.synthetic):
        parser.error("give exactly one of: a photo path, or --synthetic")
    if args.photo is not None and not args.photo.is_file():
        parser.error(f"photo not found: {args.photo}")

    keep_names, replace_names = split_names(args.keep), split_names(args.replace)
    try:
        check_flag_overlap(keep_names, replace_names)
    except PreferenceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

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
        "price_tier": args.price_tier,
        "lookup_alternates": True if args.lookup_alternates else None,
        "max_revisions": args.revisions,
        "model_proposal": args.proposal_model,
        "effort_proposal": args.proposal_effort,
        "proposal_prompt_version": args.proposal_prompt_version,
        "freeplay_proposal_prompt_version": args.proposal_prompt_version,
        "rank_prompt_version": args.rank_prompt_version,
        "freeplay_rank_prompt_version": args.rank_prompt_version,
    }
    try:
        config = replace(config, **{k: v for k, v in overrides.items() if v is not None})
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.items is not None and not 2 <= args.items <= config.max_items:
        print(
            f"ERROR: --items must be between 2 and {config.max_items}, got {args.items}",
            file=sys.stderr,
        )
        return 2
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    trace_path = point_trace_at(OUT_ROOT, run_id)
    photo = synthetic_room_image() if args.synthetic else args.photo
    try:
        result = run_spike(
            photo,
            budget_usd=args.budget,
            style=args.style or "",
            perception_only=args.perception_only,
            with_proposal=not args.no_proposal,
            keep=keep_names,
            replace=replace_names,
            slots=args.items if args.items is not None else args.slots,
            config=config,
            run_id=run_id,
        )
    except BudgetExceededError as exc:
        print(
            f"STOPPED: budget cap hit ({exc}). Nothing was truncated; raise the cap deliberately.",
            file=sys.stderr,
        )
        return 2
    except PreferenceError as exc:  # names come from perception, so this surfaces after it ran
        print(f"ERROR: {exc}", file=sys.stderr)
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
