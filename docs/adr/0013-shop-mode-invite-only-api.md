# 0013. Shop Mode as an invite-only JSON API on Fly

- **Status:** proposed
- **Date:** 2026-10-30

## Context

Shop Mode ran only on the owner's machine (127.0.0.1, JSON session files, no login). It is to be shown on the portfolio site (Vercel, React `/shop` page) to invited
people. Every search and Shop Again costs real provider money, so access and spend have to be bounded before anything is reachable from the internet.

## Decision

1. **One Fly Machine, API only.** In deployment mode the service serves `GET /healthz` and a JSON API under `APP_API_PREFIX` (`/api/shop`); no HTML or static files. The machine
   auto-stops when idle. Jobs (a search, a Shop Again) stay in memory, so the service must be a single machine; the page polls `GET /job/{id}`, so a slow Shop Again is many short requests.
2. **The portfolio forwards, the browser never talks to Fly.** A Vercel rewrite of `/api/shop/*` adds `x-origin-secret`. The service answers 403 with an empty body without it
   (`hmac.compare_digest`), and trusts `X-Forwarded-*` only after the secret passed. No CORS headers: nothing cross-origin is allowed. Host, Origin and Referer are checked.
3. **Invite codes, not accounts.** `shop-` plus 192 random bits, stored only as a SHA-256 hash; login sets an HttpOnly, SameSite=Lax cookie (Secure over https) scoped to the API path,
   14 days, also stored only as a hash. Login is rate limited (8 per address and 60 global per 10 minutes). A session belongs to the invite that made it; another caller gets 404, not 403.
4. **Hosted sessions in Postgres (Neon), hand-written SQL, psycopg 3.** One `SessionBackend` interface with the JSON files (local, unchanged) and a SQL store. The same SQL runs on SQLite in tests.
   Migrations are an idempotent versioned list; the database call retries three times (Neon suspends when idle).
5. **Three spend caps and a kill switch.** $0.50 per session, $1.00 per invite per day, $3.00 per day overall, 8 searches and 25 Shop Agains per session (`thresholds.py`, guesses to tune
   after live use). A check before work starts plus a meter in the same transaction as the session save, so counters survive restarts; background jobs honor the caps. Breach answers 429 `cap_reached` with
   `limit` and `resets_at`. `APP_SHOP_DISABLED=1` answers 503 `maintenance`.
6. **Outbound safety.** The thumbnail fetcher resolves a host once, blocks private, loopback, link-local, Fly-internal (fdaa::/16) and IPv4-mapped addresses, connects to the pinned address with the
   original Host and SNI, and refuses any redirect to another host (this closes DNS rebinding).
7. **Local use is untouched.** With no deployment settings: 127.0.0.1, the old `/api` routes and page, JSON files, no login. One route table drives both modes.

## Consequences

- Provider-side limits (Anthropic Console, Serper balance) remain the outer layer; the app's caps are not a billing guarantee.
- One machine and in-memory jobs mean a restart loses jobs in flight (the session is kept; the visitor searches again).
- Thumbnails are not served by this API (images load from provider https URLs), so no cache exception was needed: every response is `no-store`.
- Not verified here: real Neon, Fly, Vercel (see `docs/deploy.md`, "Smoke test").

## Found in the first deployment (stage D1.1)

Every search on the real Fly machine failed within a second with a generic 500 and nothing in the log. Cause: `tracing.span` appended to the relative `runs/trace.jsonl`; the container runs as the
non-root user `app`, but `/app` (made by `WORKDIR`) was owned by root, so the job raised `PermissionError`. Tests and diagnostics had run as a user who could write there, and the smoke had tracing disabled.
Stopgap: the Fly secret `APP_TRACING_DISABLED=1`. Fixes: a trace write failure (`OSError`) is reported once on stderr and never fails a call; `/app` is owned by `app`; the image and `fly.toml` set
`APP_TRACING_DISABLED=1` (traces are useless on a disposable disk); a failed job or an unexpected handler error now logs one redacted line (class, message cut to 300 characters, repository file and line;
no ids, bodies, cookies or traces) while the client keeps the generic sentence; `make deploy-smoke` runs a search as a process whose working directory is read-only with tracing on, and `make deploy-check`
checks the Dockerfile and `fly.toml` for these settings.
