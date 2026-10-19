"""Cost-and-latency spike: photo in, perception + retrieval out, per-stage table printed.

    python scripts/spike.py spike-photos/living-room.jpg --budget 800 --style "warm minimalist"

Defaults to mock (APP_LLM_MODE=mock): no network, no API key. The same script runs live with only
an env change (`make spike-live ARGS=...`, Max's call). Output goes to the gitignored
spike-output/<run_id>/ (result.json, table.txt, trace.jsonl). Photos belong in spike-photos/.
Pass `--synthetic` instead of a photo path to use a generated placeholder image.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import BudgetExceededError, Config, load_env
from app.images import synthetic_room_image
from app.llm import LLMOutputError
from app.schemas import SchemaError
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
    parser.add_argument("--budget", type=float, required=True, help="total furniture budget, USD")
    parser.add_argument("--style", required=True, help='style goal, e.g. "warm minimalist"')
    parser.add_argument(
        "--slots", type=int, default=None, help="furniture slots to search (default: config)"
    )
    args = parser.parse_args(argv)
    if (args.photo is None) == (not args.synthetic):
        parser.error("give exactly one of: a photo path, or --synthetic")
    if args.photo is not None and not args.photo.is_file():
        parser.error(f"photo not found: {args.photo}")

    load_env()
    config = Config.from_env()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    trace_path = point_trace_at(OUT_ROOT, run_id)
    photo = synthetic_room_image() if args.synthetic else args.photo
    try:
        result = run_spike(
            photo,
            budget_usd=args.budget,
            style=args.style,
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
    except (SchemaError, LLMOutputError) as exc:
        print(f"FAILED: unusable model output: {exc}", file=sys.stderr)
        return 3
    out_dir = write_outputs(result, OUT_ROOT)
    print(format_table(result))
    print(f"\nwrote {out_dir}/result.json, table.txt, and {trace_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
