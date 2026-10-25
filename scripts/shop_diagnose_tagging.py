"""Diagnose candidate tagging against the real API (owner-run; costs about $0.003).

    make shop-diagnose

Takes the titles of the most recent session in shop-data/ (or, with none, the authored pool of
tests/fixtures/shop/owner_session_replay_4.json) and sends the candidate tagging call three ways:

  A  the text-rows format (structured output) with 100 titles   (the stage 2e/2f call)
  B  the same with 40 titles
  C  a forced tool call (`record_tags`, strict JSON schema) with 40 titles   (the stage 2g call)

For each call it prints only: the model id, max_tokens, latency, how the call ended (ok, or the
exception class, its redacted message and where it was raised), the content block types, whether a
thinking block was present (and the thinking tokens when the API reports them), the stop reason, token
usage (or "unknown" with an estimated cost for a failed call that reported none), how many rows or
items were parsed, for each call that parsed rows a "fields filled" count per field (shape, material,
finish, frame_color, style: how much a title-only tagger can read), and the first 300 characters of
any text. It never prints keys, headers or the environment.
Read it with "How to read the diagnostic" in docs/adr/0012-two-modes-shop-first.md. `--mock` runs the
same three calls against the free mock client (to see the layout).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.config import Config, load_env
from app.llm import Budget
from app.shop.candidates import MODE_ROWS, MODE_TOOL, Taggable, run_call
from app.shop.clock import Clock
from app.shop.redact import describe_exception
from app.shop.store import DEFAULT_ROOT
from app.shop.vocab import DIMENSIONS

ROOT = Path(__file__).resolve().parents[1]
RAW_CHARS = 300
COST_NOTE = (
    "Expected cost: about $0.003 in all (three calls of 100, 40 and 40 titles, Haiku-class)."
)
VARIANTS = (
    ("A rows, 100 titles", MODE_ROWS, 100),
    ("B rows, 40 titles", MODE_ROWS, 40),
    ("C forced tool call, 40 titles", MODE_TOOL, 40),
)


def latest_titles(root: Path, limit: int = 100) -> tuple[list[str], str]:
    """Titles from the newest session file under `root`, else from the replay-4 fixture pool."""
    files = (
        sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if root.is_dir()
        else []
    )
    for path in files:
        try:
            data = json.loads(path.read_text())
            titles = [r["title"] for r in data.get("results", {}).values() if r.get("title")]
        except (OSError, ValueError, KeyError, AttributeError):
            continue
        if titles:
            return titles[:limit], f"session file {path.name}"
    fixture = ROOT / "tests" / "fixtures" / "shop" / "owner_session_replay_4.json"
    pool = json.loads(fixture.read_text())["pool"]
    return [x["title"] for x in pool][:limit], "the replay-4 fixture pool (no session file found)"


def fields_filled(rows: dict[int, dict[str, str | None]], items: int) -> list[str]:
    """For each field, how many of the items got a value (not null, not `other`): how much a
    title-only tagger can read."""
    out = []
    for dim in DIMENSIONS:
        filled = sum(1 for tags in rows.values() if tags.get(dim) not in (None, "other"))
        out.append(f"{dim} {filled}/{items}")
    return out


def describe(
    label: str, entry: dict[str, object], rows: dict[int, dict[str, str | None]] | None = None
) -> list[str]:
    outcome = "ok" if not entry["error"] else f"FAILED: {entry['error']}"
    if entry.get("usage_known", True):
        usage = f"{entry['input_tokens']} input tokens, {entry['output_tokens']} output tokens"
    else:
        usage = (
            f"unknown (the response reported none); cost ${entry['usd']:.5f} is "
            "estimated (failed call), from the max_tokens ceiling"
        )
    thinking = "yes" if entry.get("had_thinking") else "no"
    if entry.get("thinking_tokens") is not None:
        thinking += f" ({entry['thinking_tokens']} thinking tokens reported)"
    lines = [
        f"--- {label}",
        f"  model:        {entry['model']}",
        f"  max_tokens:   {entry['max_tokens']}   (per-request timeout {entry['timeout_s']} s)",
        f"  latency:      {entry['seconds']} s",
        f"  outcome:      {outcome}",
        *([f"  raised at:    {entry['where']}"] if entry.get("where") else []),
        f"  block types:  {', '.join(entry['block_types']) or '(none)'}",  # type: ignore[arg-type]
        f"  thinking:     {thinking}",
        f"  stop reason:  {entry['stop_reason'] or '(none)'}",
        f"  usage:        {usage}",
        f"  parsed:       {entry['rows_parsed']} of {entry['items']} (valid JSON: {entry['json_ok']})",
    ]
    if rows is not None and rows:
        lines.append(f"  fields filled: {', '.join(fields_filled(rows, int(str(entry['items']))))}")
    lines.append(f"  text:         {entry['raw'] or '(no text)'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_ROOT, help="session folder")
    parser.add_argument("--mock", action="store_true", help="use the free mock client")
    args = parser.parse_args(argv)
    print("Candidate tagging diagnostic")
    print(COST_NOTE if not args.mock else "Mock mode: free.")
    if not args.mock:
        load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "ERROR: ANTHROPIC_API_KEY is not set (its value is never printed).", file=sys.stderr
            )
            return 2
    base = Config.from_env()
    config = Config(
        **{**base.__dict__, "llm_mode": "mock" if args.mock else "live", "tracing_disabled": True}
    )
    titles, source = latest_titles(args.data_dir)
    print(f"Titles from {source}: {len(titles)} available.")
    from app.shop.candidates import TagBook

    for label, mode, n in VARIANTS:
        batch = [
            Taggable(f"d{i}", titles[i % len(titles)], None, "Seller") for i in range(min(n, 100))
        ]
        try:
            rows, entry = run_call(
                batch, TagBook(), Budget(config), mode, False, Clock(), 30.0, raw_chars=RAW_CHARS
            )
            lines = describe(label, entry, rows)
        except Exception as exc:  # the redacted message and where it was raised in this repository
            text, where = describe_exception(exc)
            lines = [
                f"--- {label}",
                "  outcome:      FAILED before a response",
                f"  exception:    {text}",
                f"  raised at:    {where or '(outside this repository)'}",
            ]
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
