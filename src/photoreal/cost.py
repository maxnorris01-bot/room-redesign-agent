"""The plan (calls, models, estimated dollars) and the running-total cap."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from photoreal.specs import (
    ANTHROPIC_VISION_CALL_USD_ASSUMED,
    ProviderSpec,
    estimate_call_usd,
)


class CapError(RuntimeError):
    """The next call would take the run past --max-usd."""


@dataclass
class PlannedCall:
    index: int
    kind: str  # "locate" | "render" | "judge"
    provider: str
    model: str
    items: list[str]
    n_images: int
    size: str
    quality: str
    est_usd: float
    basis: str


@dataclass
class Plan:
    calls: list[PlannedCall]

    @property
    def total(self) -> float:
        return round(sum(c.est_usd for c in self.calls), 4)

    def to_json(self) -> dict[str, object]:
        return {"calls": [asdict(c) for c in self.calls], "total_est_usd": self.total}


def make_plan(
    spec: ProviderSpec,
    slots: list[str],
    *,
    mode: str,
    quality: str,
    size: str,
    locate: bool,
    judge: bool,
    anthropic_model: str,
    env: Mapping[str, str] | None = None,
) -> Plan:
    calls: list[PlannedCall] = []

    def add(
        kind: str,
        provider: str,
        model: str,
        items: list[str],
        n_images: int,
        usd: float,
        basis: str,
    ) -> None:
        calls.append(
            PlannedCall(
                len(calls) + 1, kind, provider, model, items, n_images, size, quality, usd, basis
            )
        )

    if locate:
        add(
            "locate",
            "anthropic",
            anthropic_model,
            slots,
            1,
            ANTHROPIC_VISION_CALL_USD_ASSUMED,
            "assumed",
        )
    groups = [slots] if mode == "all-at-once" else [[s] for s in slots]
    for group in groups:
        n_images = 1 + len(group)
        usd, basis = estimate_call_usd(
            spec.approach, n_input_images=n_images, quality=quality, env=env
        )
        add("render", spec.approach, spec.model, group, n_images, usd, basis)
    if judge:
        add(
            "judge",
            "anthropic",
            anthropic_model,
            slots,
            2 + len(slots),
            ANTHROPIC_VISION_CALL_USD_ASSUMED,
            "assumed",
        )
    return Plan(calls)


class SpendGuard:
    """Running total against the hard cap. `check` before a call, `add` after it."""

    def __init__(self, cap_usd: float) -> None:
        self.cap = cap_usd
        self.spent = 0.0

    def check(self, est_usd: float) -> None:
        if self.spent + est_usd > self.cap + 1e-9:
            raise CapError(
                f"stopping before the next call: ${self.spent:.3f} spent + ${est_usd:.3f} estimated "
                f"would pass the cap of ${self.cap:.2f}"
            )

    def add(self, usd: float) -> None:
        self.spent += usd


def plan_text(plan: Plan, cap: float, *, live: bool) -> str:
    lines = [("LIVE" if live else "MOCK (offline)") + " render plan"]
    for c in plan.calls:
        lines.append(
            f"  {c.index}. {c.kind:<6} {c.provider}/{c.model}  items: {', '.join(c.items)}  "
            f"images: {c.n_images}  size {c.size}  quality {c.quality}  "
            f"est ${c.est_usd:.4f}  [{c.basis}]"
        )
    lines.append(f"  estimated total ${plan.total:.4f} against --max-usd ${cap:.2f}")
    lines.append("  prices are assumptions to check on each vendor dashboard")
    return "\n".join(lines)
