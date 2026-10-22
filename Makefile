.PHONY: install lint format typecheck test eval-fast eval-fast-live eval-standard eval-standard-live \
	eval-nightly eval-nightly-live spike spike-live
 
install:
	uv sync
 
lint:
	uv run ruff check .
	uv run ruff format --check .
 
format:
	uv run ruff check --fix .
	uv run ruff format .
 
typecheck:
	uv run mypy
 
test:
	uv run pytest
 
# Free, no API key needed: exercises routing/shape/tracing/budget against a mock client.
# Not a quality signal - see evals/run.py's docstring. This is the default; APP_LLM_MODE is
# forced here so a stray `APP_LLM_MODE=live` in .env can't make a "fast" run silently cost money.
eval-fast:
	APP_LLM_MODE=mock uv run python -m evals.run --tier fast
 
# Costs real API money. Run deliberately, not routinely.
eval-fast-live:
	APP_LLM_MODE=live uv run python -m evals.run --tier fast
 
# Standard and nightly tiers follow the same split as fast: the plain target forces mock (free),
# the -live target forces live (real API cost; CI schedules and Max run those deliberately).
eval-standard:
	APP_LLM_MODE=mock uv run python -m evals.run --tier standard

# Costs real API money.
eval-standard-live:
	APP_LLM_MODE=live uv run python -m evals.run --tier standard

eval-nightly:
	APP_LLM_MODE=mock uv run python -m evals.run --tier nightly

# Costs real API money.
eval-nightly-live:
	APP_LLM_MODE=live uv run python -m evals.run --tier nightly

# Cost/latency spike (SPEC.md "Session 1"). Pass arguments with ARGS, e.g.
#   make spike ARGS='spike-photos/living-room.jpg --budget 800 --style "warm minimalist"'
# Mock is forced: no network, no key required. With no ARGS, a generated image is used in freeplay (no budget).
SPIKE_MOCK_DEFAULT_ARGS = --synthetic --style "warm minimalist"
spike:
	APP_LLM_MODE=mock uv run python scripts/spike.py $(or $(ARGS),$(SPIKE_MOCK_DEFAULT_ARGS))

# Costs real API money. Run deliberately (Max's call), after the Console spend limit exists.
spike-live:
	APP_LLM_MODE=live uv run python scripts/spike.py $(ARGS)
