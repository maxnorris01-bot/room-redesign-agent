"""Build the HTML report for one run, the latest run, or every run plus an index.

    python scripts/report.py                       # latest run under spike-output/
    python scripts/report.py spike-output/<run id> # one run
    python scripts/report.py --all                 # every run, then spike-output/index.html

Prints the file path and the macOS command to open it; it does not open anything itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.report import run_dirs, write_index, write_report

OUT_ROOT = Path("spike-output")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("run", nargs="?", type=Path, help="a run folder (default: the latest)")
    parser.add_argument("--all", action="store_true", help="every run, plus the index page")
    parser.add_argument(
        "--root", type=Path, default=OUT_ROOT, help="runs folder (default spike-output)"
    )
    args = parser.parse_args(argv)
    if args.all and args.run is not None:
        parser.error("give a run folder or --all, not both")
    if not args.root.is_dir():
        print(f"ERROR: {args.root} not found", file=sys.stderr)
        return 2
    if args.all:
        failed = 0
        for run_dir in run_dirs(args.root):
            try:
                write_report(run_dir)
            except Exception as exc:  # one odd run must not stop the rest
                failed += 1
                print(
                    f"WARNING: no report for {run_dir.name}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        index = write_index(args.root)
        print(f"wrote {index}" + (f" ({failed} runs skipped)" if failed else ""))
        print(f"open {index}")
        return 0
    run_dir = args.run
    if run_dir is None:
        runs = run_dirs(args.root)
        if not runs:
            print(f"ERROR: no runs in {args.root}", file=sys.stderr)
            return 2
        run_dir = runs[-1]
    if not (run_dir / "result.json").is_file():
        print(f"ERROR: {run_dir}/result.json not found", file=sys.stderr)
        return 2
    path = write_report(run_dir)
    print(f"wrote {path}")
    print(f"open {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
