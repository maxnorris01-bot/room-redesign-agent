# 0004. Cost and latency targets for the measured run

- **Status:** proposed (numbers below are a draft for the owner to accept or change before the measured 3-room run; nothing is locked or enforced yet)
- **Date:** 2026-10-21

## Context

SPEC.md says to run one exploratory room, record it ungraded, then lock targets in an ADR before the measured 3-room run, and never loosen them after seeing a result. The earlier draft targets (perception plus retrieval at or under $0.08 and 30 s; full run at or under $0.20 mean and p95 60 s) were written before any live run. Since then:

- The owner said a required wait of a minute is too long and 20 s is the maximum, then relaxed it to "judge the wait by feel on real runs". The visitor-questions-while-waiting idea is parked in the backlog and used only if the wait feels too long.
- Perception prompt v2 cut perception from a typical 13 to 17 s to about 9 s (one run each; noise about 4 s).
- Retrieval moved from web search to structured shopping data (ADR 0005). One live run measured 14.8 s for perception plus retrieval, one slot, real Anthropic cost about $0.025.
- Provider calls (search and lookup) are billed by the provider, not Anthropic, and mostly come out of free tiers now.

Evidence: `docs/sessions/2026-10-21-live-runs-latency-and-shopping.md`.

## Definitions

- **Wait** is wall-clock time from upload to the last stage's result, taken from the run's measured critical path (`critical_path_seconds`), with retrieval slots concurrent.
- **First feedback** is the time until the visitor sees the first stage result or progress message.
- **Anthropic cost** is the sum of model-call dollars. **Provider cost** is search and lookup calls at assumed list prices (`APP_SERPER_USD_PER_QUERY`, `APP_SERPAPI_USD_PER_SEARCH`), reported separately because free-tier use is $0 in reality.

## Proposed targets (for the measured 3-room run)

| Target | Value | Basis |
|---|---|---|
| Perception plus retrieval wait, p95 over the rooms | at or under 20 s | One run: 14.8 s. Owner's stated maximum. |
| Full-run wait (perception, retrieval, proposal, self-check), p95 | at or under 35 s | Guess: 14.8 s plus about 10 s proposal plus about 10 s self-check. Proposal and self-check do not exist yet, so this is the least grounded number. |
| First feedback | at or under 3 s | Perception time to first token was 1.5 to 2.4 s. Needs stage progress streamed to the UI. |
| Anthropic cost per full run, mean | at or under $0.10 | Perception about $0.025, rank about $0.002 per slot, so about $0.06 allowance for proposal and self-check. |
| Provider cost per live run at list price | at or under $0.20 | Three slots with two finalists is about $0.15 at the assumed prices; $0 on free tiers. |
| Perception output | at or under 1,500 tokens | v2 runs: 1,192 to 1,350. |

Quality targets belong to the eval design in SPEC.md, not here.

## Rules

- Targets are written to `thresholds.yaml` in their own commit, before the measured run, and never loosened after seeing a result. A target can only be changed by a new ADR that says why, dated before the next measured run.
- Report medians and the worst run, not just means; at least 3 rooms of different difficulty, with repeated runs per room because perception output varies run to run (room size, keepable flags).
- Estimated provider dollars are labelled as estimates in every table.

## Consequences

- Anthropic and estimated provider dollars now have separate caps (ADR 0006): `APP_MAX_COST_USD` (0.25) bounds Anthropic dollars only, and `APP_MAX_PROVIDER_COST_USD` (0.30) bounds estimated provider dollars. A default shopping run estimates about $0.15 of provider dollars (six lookups at the assumed price), which no longer competes with the Anthropic budget. Both caps are enforced; the table and `result.json` show the two subtotals. (This replaces the earlier note that provider estimates counted toward `APP_MAX_COST_USD`.)
- The full-run wait cannot be judged until proposal and self-check exist; the first full-pipeline run may force a change of this ADR, written before the measured run.
- If the wait feels too long in practice, the first lever is the parked idea of asking the visitor questions while perception runs; others are a smaller image (`--image-long-edge`), lower effort, fewer finalists, and streaming partial results.

## Owner answers (2026-10-21)

1. Leave the targets as drafted for now (20 s perception plus retrieval, 35 s full run). Still `proposed`: they are locked in `thresholds.yaml` only before the measured run.
2. Provider cost: left as a target as drafted; the owner asked what it means. It is the search and lookup calls at assumed list prices, reported separately from Anthropic cost because free tiers make the real cost $0. Open: keep it as a target or only report it.
3. The 14.8 s wait (perception plus one retrieval slot) felt fine for now, so the visitor-questions idea stays in the backlog.
