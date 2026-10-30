.PHONY: install lint format typecheck test eval-fast eval-fast-live eval-standard eval-standard-live \
	eval-nightly eval-nightly-live spike spike-live report reports render render-live shop shop-live shop-check shop-diagnose \
	eval-shop-plan eval-shop-mock eval-shop-live eval-shop-report eval-shop-rejudge deploy-check deploy-smoke
 
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

# HTML report for a run (no network, nothing is opened for you). Latest run, or RUN=spike-output/<run id>.
report:
	APP_LLM_MODE=mock uv run python scripts/report.py $(RUN)

# Reports for every run under spike-output/, plus spike-output/index.html.
reports:
	APP_LLM_MODE=mock uv run python scripts/report.py --all

# Photo-real spike (an experiment, see docs/adr/0011). Mock and offline: placeholder images stand in
# for the renders and for any missing product image. Pass arguments with ARGS.
render:
	APP_LLM_MODE=mock uv run python scripts/render_spike.py $(or $(ARGS),--from-run latest --approach gemini --locate --judge)

# Real, billed calls (Max's call). Needs --live arguments, the provider key in the environment, and
# product images in render-inputs/. The plan and its estimated cost print first; --max-usd is a hard stop.
render-live:
	APP_LLM_MODE=live uv run python scripts/render_spike.py --live $(ARGS)

# Shop Mode (ADR 0012): a local product search page. Mock: fixture results, no network, no key.
# Prints the URL; stop with Ctrl-C. Pass a port with ARGS='--port 9000'.
shop:
	APP_LLM_MODE=mock uv run python scripts/shop.py $(ARGS)

# Costs real money (Anthropic, Serper, SerpApi); the script prints the expected cost per search first.
shop-live:
	APP_LLM_MODE=live uv run python scripts/shop.py $(ARGS)

# Headless browser check in mock mode: starts and stops its own server, writes screenshots to
# spike-output/shop-screenshots/. Playwright is supplied for this run only (not a project dependency);
# it needs a Chromium headless shell already installed.
shop-check:
	APP_LLM_MODE=mock uv run --with playwright python scripts/shop_browser_check.py

# LIVE, owner-run, about $0.003: sends the candidate tagging call three ways against the real API and
# prints only model, max_tokens, latency, outcome, block types, stop reason, usage, rows parsed and the
# first 300 characters of any text. Never prints keys or the environment.
shop-diagnose:
	APP_LLM_MODE=live uv run python scripts/shop_diagnose_tagging.py $(ARGS)

# Shop Mode eval (stage 3, evals/shop/README.md). Not wired into eval-fast: that target runs the
# template's placeholder pipeline; this one is its own harness with its own cases and thresholds.
# Lists cases, personas, the thresholds hash and the estimated cost and credits. Spends nothing.
eval-shop-plan:
	APP_LLM_MODE=mock uv run python -m evals.shop plan $(if $(CASES),--case $(CASES),)

# The whole harness on fixtures and deterministic judges. Free, no key; safe for CI. Mock numbers
# show that the harness works, not how good Shop Mode is.
eval-shop-mock:
	APP_LLM_MODE=mock uv run python -m evals.shop mock $(if $(CASES),--case $(CASES),) $(ARGS)

# Costs real money (Anthropic and Serper). Refuses to start unless the command line says
# EVAL_SHOP_CONFIRM=yes, and never runs under CI or tests:
#   make eval-shop-live EVAL_SHOP_CONFIRM=yes
# A few cases: CASES=case-04,case-06 (plan, mock, live and rejudge all take it).
eval-shop-live:
	EVAL_SHOP_SERPER_CACHE=$(or $(EVAL_SHOP_SERPER_CACHE),record) EVAL_SHOP_CONFIRM=$(EVAL_SHOP_CONFIRM) APP_LLM_MODE=live uv run python -m evals.shop live $(if $(CASES),--case $(CASES),) $(ARGS)

# ZERO SPEND, no network: all 12 cases (or CASES=...) on the saved Serper responses (a miss is an error),
# the mock model, persona and judges. Structural checks only: not a quality result. If nothing is saved
# yet, run a live eval once (it records by default).
eval-shop-replay:
	APP_LLM_MODE=mock uv run python -m evals.shop replay $(if $(CASES),--case $(CASES),)

# ZERO SPEND: list anomalies in a saved live results file, most severe first:
#   make eval-shop-lint RESULTS=evals/shop/results/shop-live-<run>.json
eval-shop-lint:
	APP_LLM_MODE=mock uv run python -m evals.shop lint --results $(RESULTS)

# Markdown report from a results file (default: the newest). Pass RESULTS=path and OUT=path.
eval-shop-report:
	APP_LLM_MODE=mock uv run python -m evals.shop report $(RESULTS) $(if $(OUT),--out $(OUT),)

# Judge noise: re-run ONLY the persona judge on the batches saved in a live results file and report how
# often its answers agree with the first run. Judge calls only (no search, no pipeline); prints the
# estimate first and refuses like the live target (needs EVAL_SHOP_CONFIRM=yes, never under CI or tests):
#   make eval-shop-rejudge EVAL_SHOP_CONFIRM=yes RESULTS=evals/shop/results/shop-live-<run>.json
# Narrow it with ARGS='--rounds 1,2 --cases case-06,case-09'.
eval-shop-rejudge:
	EVAL_SHOP_CONFIRM=$(EVAL_SHOP_CONFIRM) APP_LLM_MODE=live uv run python -m evals.shop rejudge --results $(RESULTS) $(if $(CASES),--cases $(CASES),) $(ARGS)

# Obvious mistakes in the Dockerfile, .dockerignore and fly.toml, without running docker or fly.
deploy-check:
	uv run python -m app.shop.deploycheck

# The deployment-mode server on a temporary SQLite database and mock providers: /healthz, a 401, a
# login, an authenticated call, a cap_reached and the maintenance answer. No network, no docker.
deploy-smoke:
	APP_LLM_MODE=mock uv run python scripts/deploy_smoke.py
