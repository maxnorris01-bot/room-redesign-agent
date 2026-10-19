"""The cost-and-latency spike pipeline (SPEC.md "Session 1").

Stages: prepare_image -> perception -> retrieval.

`run_spike` is the testable core; `scripts/spike.py` is a thin CLI around it. Nothing here enforces
the proposed cost/latency targets: they are printed as informational comparisons only (they are
not locked until an ADR says so - see SPEC.md and docs/working-notes-and-decisions.md).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import tracing
from app.config import Config
from app.images import PreparedImage, prepare_image
from app.llm import Budget, Completion
from app.perception import PerceptionResult, perceive
from app.pricing import is_priced
from app.retrieval import RetrievalResult, retrieve
from app.tracing import span

# PROPOSED targets from SPEC.md, not locked. Informational only: never enforced, never in
# evals/thresholds.yaml until an ADR locks them.
PROPOSED_TARGETS = {
    "perception_plus_retrieval_cost_usd": 0.08,
    "perception_plus_retrieval_seconds": 30.0,
    "full_run_mean_cost_usd": 0.20,
    "full_run_p95_seconds": 60.0,
}


@dataclass
class StageRow:
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    web_searches: int
    cost_usd: float
    seconds: float


@dataclass
class SpikeResult:
    run_id: str
    llm_mode: str
    started_at: str
    photo_name: str
    budget_usd: float
    style: str
    prompt_versions: dict[str, str]
    models: dict[str, str]
    image: dict[str, Any]
    rows: list[StageRow] = field(default_factory=list)
    perception: dict[str, Any] = field(default_factory=dict)
    retrieval: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    unpriced_models: list[str] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.rows)

    @property
    def total_seconds(self) -> float:
        return sum(r.seconds for r in self.rows)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["totals"] = {"cost_usd": self.total_cost_usd, "seconds": self.total_seconds}
        data["proposed_targets_informational"] = PROPOSED_TARGETS
        return data


def _row(stage: str, c: Completion, seconds: float) -> StageRow:
    return StageRow(
        stage, c.model, c.input_tokens, c.output_tokens, c.web_searches, c.cost_usd, seconds
    )


def run_spike(
    photo: Path | bytes,
    *,
    budget_usd: float,
    style: str,
    slots: int | None = None,
    config: Config | None = None,
    run_id: str | None = None,
) -> SpikeResult:
    """Run the three stages under one shared `Budget` (step + cost caps apply to the whole run)."""
    config = config or Config.from_env()
    budget = Budget(config)
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    n_slots = slots if slots is not None else config.default_slots

    result = SpikeResult(
        run_id=run_id,
        llm_mode=config.llm_mode,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        photo_name=photo.name if isinstance(photo, Path) else "<bytes>",
        budget_usd=budget_usd,
        style=style,
        prompt_versions={},
        models={"perception": config.model_perception, "retrieval": config.model_retrieval},
        image={},
    )
    result.unpriced_models = [m for m in set(result.models.values()) if not is_priced(m)]

    with span("spike.run", config=config, run_id=run_id, llm_mode=config.llm_mode) as run_rec:
        t0 = time.perf_counter()
        with span("stage.prepare_image", config=config, run_id=run_id) as img_rec:
            prepared: PreparedImage = prepare_image(photo, max_edge=config.image_max_edge)
            img_rec.update(prepared.summary())
        result.image = prepared.summary()
        result.rows.append(StageRow("prepare_image", "-", 0, 0, 0, 0.0, time.perf_counter() - t0))

        t0 = time.perf_counter()
        perceived: PerceptionResult = perceive(prepared, budget=budget, run_id=run_id)
        result.rows.append(_row("perception", perceived.completion, time.perf_counter() - t0))
        result.prompt_versions["perception"] = perceived.prompt_version
        result.perception = perceived.perception.model_dump()

        retrieved: RetrievalResult = retrieve(
            perceived.perception,
            budget_usd=budget_usd,
            style=style,
            max_slots=n_slots,
            budget=budget,
            run_id=run_id,
        )
        result.prompt_versions["retrieval"] = retrieved.prompt_version
        for sr in retrieved.slots:
            result.rows.append(_row(f"retrieval: {sr.slot.category}", sr.completion, sr.seconds))
            result.retrieval.append(
                {
                    "slot": asdict(sr.slot),
                    "candidates": [c.model_dump() for c in sr.candidates],
                    "dropped_unverified": sr.dropped,
                    "note": sr.note,
                    "searches": sr.completion.searches,
                }
            )
        result.steps = budget.steps
        run_rec.update(
            total_cost_usd=round(result.total_cost_usd, 5),
            total_seconds=round(result.total_seconds, 3),
            steps=budget.steps,
        )
    return result


def format_table(result: SpikeResult) -> str:
    """Per-stage table, totals, and the informational comparison to the proposed targets."""
    headers = ["stage", "model", "in tok", "out tok", "searches", "dollars", "seconds"]
    body = [
        [r.stage, r.model, str(r.input_tokens), str(r.output_tokens), str(r.web_searches),
         f"${r.cost_usd:.4f}", f"{r.seconds:.2f}"]
        for r in result.rows
    ]  # fmt: skip
    totals = [
        "TOTAL", "",
        str(sum(r.input_tokens for r in result.rows)),
        str(sum(r.output_tokens for r in result.rows)),
        str(sum(r.web_searches for r in result.rows)),
        f"${result.total_cost_usd:.4f}", f"{result.total_seconds:.2f}",
    ]  # fmt: skip
    rows = [headers, *body, totals]
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]

    def fmt(r: list[str]) -> str:
        cells = [c.ljust(widths[i]) if i < 2 else c.rjust(widths[i]) for i, c in enumerate(r)]
        return "  ".join(cells)

    rule = "  ".join("-" * w for w in widths)
    lines: list[str] = []
    if result.llm_mode == "mock":
        lines += [
            "*** MOCK MODE: no API call was made. Tokens, dollars and seconds below are fake",
            "*** plumbing values, not measurements. Candidates are fake (retailer MOCK).",
            "",
        ]
    lines += [
        f"run {result.run_id}  mode={result.llm_mode}  photo={result.photo_name}  "
        f"budget=${result.budget_usd:.0f}  style={result.style!r}",
        f"prompts: {result.prompt_versions}  image: {result.image.get('width')}x"
        f"{result.image.get('height')} (from {result.image.get('original_width')}x"
        f"{result.image.get('original_height')})  steps: {result.steps}",
        "",
        fmt(headers), rule, *[fmt(r) for r in body], rule, fmt(totals),
    ]  # fmt: skip
    if result.unpriced_models:
        lines += [
            "",
            f"WARNING: no price for {result.unpriced_models}; costed at the highest known rate",
        ]

    pr = [r for r in result.rows if r.stage == "perception" or r.stage.startswith("retrieval")]
    pr_cost = sum(r.cost_usd for r in pr)
    pr_secs = sum(r.seconds for r in pr)
    t = PROPOSED_TARGETS
    lines += [
        "",
        "Proposed targets, informational only (not locked, not enforced; SPEC.md):",
        f"  perception+retrieval cost : ${pr_cost:.4f}  "
        f"vs proposed <= ${t['perception_plus_retrieval_cost_usd']:.2f}",
        f"  perception+retrieval time : {pr_secs:.1f}s  "
        f"vs proposed <= {t['perception_plus_retrieval_seconds']:.0f}s",
        f"  full-run mean cost / p95  : n/a for one run "
        f"(proposed <= ${t['full_run_mean_cost_usd']:.2f} mean, "
        f"p95 <= {t['full_run_p95_seconds']:.0f}s)",
    ]
    return "\n".join(lines)


def write_outputs(result: SpikeResult, out_root: Path) -> Path:
    """Write result.json and table.txt under `out_root/<run_id>/` (trace.jsonl goes there too)."""
    out_dir = out_root / result.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(result.to_json(), indent=2, default=str) + "\n")
    (out_dir / "table.txt").write_text(format_table(result) + "\n")
    return out_dir


def point_trace_at(out_root: Path, run_id: str) -> Path:
    """Send this run's trace lines to `out_root/<run_id>/trace.jsonl`."""
    path = out_root / run_id / "trace.jsonl"
    tracing.TRACE_PATH = path
    return path
