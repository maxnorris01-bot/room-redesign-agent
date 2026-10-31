# Deploying Shop Mode (invite-only JSON API on Fly)

Shop Mode runs as one Python service on a single Fly Machine. In deployment it is API only: it serves `GET /healthz` and a JSON
API under `/api/shop`, nothing else (no HTML, no `app.js`, no CSS). The portfolio site (Vercel, React) gets its own `/shop` page
and forwards `/api/shop/*` to this service, so the browser only ever talks to the portfolio's domain. Sessions live in a small
hosted Postgres (Neon); invite codes, login sessions and spend counters live in the same database. The contract the React page
uses is `docs/api-contract.md`; the decisions are ADR 0013.

Nothing in this repository has been run against a real Fly app, a real Neon database or a real Vercel deployment. The tests use mock
providers and SQLite for the same SQL; the list in "Smoke test" is what only the real services can prove.

## Settings and secrets (names only; you type the values)

Secrets (`fly secrets set NAME=...`, typed by you, never in a file, never pasted into a chat or a commit):

| Name | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | the Anthropic key (set a spend limit in the Anthropic Console, see below) |
| `SERPER_API_KEY` | Serper (Google Shopping search) |
| `SERPAPI_API_KEY` | SerpApi (product lookups for Open and More photos) |
| `DATABASE_URL` | the Neon connection string (`postgresql://...?sslmode=require`) |
| `APP_ORIGIN_SECRET` | a long random string the portfolio's rewrite sends as `x-origin-secret`; the service refuses every request without it |

Settings (not secret, set with `fly secrets set` too, or in `fly.toml [env]`; `fly.toml` already holds the first five):

| Name | Value |
|---|---|
| `APP_DEPLOY_MODE` | `1` (the container sets it; locally it is unset and everything works as before) |
| `APP_LLM_MODE` | `live` (set in `fly.toml`) |
| `APP_TRACING_DISABLED` | `1`, part of the image and `fly.toml`: the service writes no trace file on Fly. If you set it as a Fly secret during the first deployment, it is no longer needed: `fly secrets unset APP_TRACING_DISABLED` |
| `APP_HOST`, `APP_PORT` | `0.0.0.0` and `8080` (set in `fly.toml` and the Dockerfile; the port must match `internal_port`) |
| `APP_API_PREFIX` | `/api/shop` |
| `APP_ALLOWED_HOSTS` | the Fly host name, for example `room-redesign-shop.fly.dev` (comma separated) |
| `APP_ALLOWED_ORIGINS` | the portfolio origin, for example `https://your-portfolio.example` (comma separated); POSTs must come from it |
| `APP_PUBLIC_URL` | the portfolio's public https URL (decides the `Secure` cookie flag) |
| `APP_CONTACT_EMAIL` | shown by `GET /auth/session` for "ask for an invite" |
| `APP_SHOP_DISABLED` | `1` is the kill switch (every call but `/healthz` and `GET /auth/session` answers 503) |
| `APP_CAP_SESSION_USD`, `APP_CAP_INVITE_DAY_USD`, `APP_CAP_GLOBAL_DAY_USD`, `APP_CAP_SEARCHES`, `APP_CAP_SHOP_AGAINS` | spend caps; defaults $0.50, $1.00, $3.00, 8 and 25 |
| `APP_ALLOW_NO_SECRET` | **never set this in production.** It exists only so the service can start without `APP_ORIGIN_SECRET` for local testing |

The service refuses to start in deployment mode without `APP_ORIGIN_SECRET`, `APP_ALLOWED_HOSTS`, `APP_ALLOWED_ORIGINS` and `DATABASE_URL`, and
says which one is missing (never a value).

## Runbook

Run these yourself, in this order. Replace the names in angle brackets.

1. **Check the files.** `make deploy-check` (no docker, no fly) and `make deploy-smoke` (the deployment-mode server on a temporary SQLite database).
2. **Create the Fly app.** `fly apps create <app-name>` and put the name in `fly.toml` (`app = "..."`).
3. **Create the database.** In the Neon console make a project (a free plan is enough), copy the connection string. Then
   `fly secrets set DATABASE_URL=...` (type the value when your shell asks; do not paste it anywhere else).
4. **Set the other secrets and settings.** `fly secrets set ANTHROPIC_API_KEY=... SERPER_API_KEY=... SERPAPI_API_KEY=... APP_ORIGIN_SECRET=...`
   (make the origin secret with `openssl rand -hex 32`), then `fly secrets set APP_ALLOWED_HOSTS=<app-name>.fly.dev APP_ALLOWED_ORIGINS=https://<portfolio-domain> APP_PUBLIC_URL=https://<portfolio-domain> APP_CONTACT_EMAIL=<you@example.org>`.
5. **One machine only.** `fly scale count 1` after the first deploy (the service keeps in-flight jobs in memory, so it must be a single machine).
6. **Deploy.** `fly deploy`. Fly builds the image from the `Dockerfile` (you do not run docker).
7. **Migrate the database.** `fly ssh console -C "python -m app.shop.admin migrate"` (it is safe to repeat; the service also migrates when it starts).
8. **Make the first invite.** `fly ssh console -C "python -m app.shop.admin invite create --label 'First friend' --days 30"`. The code is printed once and never stored.
   Send it to the person through a private channel. `invite list` shows label, created, last seen, spend today and revoked (never the code).
9. **Check health.** `curl https://<app-name>.fly.dev/healthz` should print `{"ok": true}`. Any other path without the origin secret is a 403 with an empty body.
10. **Wire the portfolio** (next section), then run the smoke test.

### Operating it

- **Logs.** `fly logs`. The service logs no request lines (paths carry session ids) and never a code, cookie or secret. A failed job or an unexpected server error prints one line, `shop error [search] PermissionError: ... at src/app/...:NN` (class, redacted message, repository location); the visitor only sees the generic sentence.
- **Revoke an invite.** `fly ssh console -C "python -m app.shop.admin invite revoke <id>"`; its logins end at once.
- **Rotate the origin secret.** `fly secrets set APP_ORIGIN_SECRET=<new>` and change the Vercel environment variable to the same value, then redeploy the portfolio. Between the two steps shop calls
  get 403: do it quickly, or turn the kill switch on first.
- **Kill switch.** `fly secrets set APP_SHOP_DISABLED=1` (the machine restarts; every call but `/healthz` and `GET /auth/session` answers 503 `maintenance`). `fly secrets unset APP_SHOP_DISABLED` to resume.
- **Check spend.** `fly ssh console -C "python -m app.shop.admin caps show"` (the caps in force and today's spend) and `invite list` (per invite today).
- **Provider-side limits (an outer layer, set them).** In the Anthropic Console set a monthly spend limit for the key this service uses. Watch the Serper balance
  (and the SerpApi plan). The app's own caps are per session, per invite and per day; these limits stop a bug or a leaked key.
- **Idle behaviour.** The machine stops when idle and starts on the next request (a few seconds). Neon suspends the database after five minutes idle; the first query after that may be slow or fail,
  and the service retries three times before answering 500.

## What the portfolio-site repo needs (not changed by this repo)

The portfolio's `vercel.json` today holds a single-page-app rewrite that excludes `/api/`. These are the changes to work out and test there:

a. **A rewrite** of `/api/shop/:path*` to `https://<app-name>.fly.dev/api/shop/:path*`, keeping the path (an external-origin rewrite). The browser keeps the portfolio URL. The origin receives the
   Fly `Host`, and Vercel adds `x-forwarded-for`, `x-real-ip`, `x-forwarded-host` and `x-forwarded-proto`; the service trusts them only on requests that carry the origin secret.
   The secret must be added as a request header `x-origin-secret` from a Vercel environment variable. Vercel's documented example for attaching a header uses `routes`, while the portfolio's
   file uses `rewrites`; the exact setup (a `routes` entry with `headers`, or a Vercel Function that forwards) has to be worked out and tested there. The new rewrite is additive and must come before
   the single-page-app catch-all. `/healthz` is not forwarded (Fly checks it directly).
b. **No caching** of those paths. The service sends `Cache-Control: no-store` and `Vary: Cookie` on every response and Vercel honors origin cache headers; also check that the platform does not
   cache the rewritten path by rule. (There is no thumbnail route, so no response is meant to be cached.)
c. **A preview-deployment test of a slow Shop Again (about 35 s).** Vercel documents no time limit for forwarded requests, so this is unverified. This service does not hold a request open for the slow
   work: a search and a Shop Again are background jobs (`POST` answers 202 with a job id and the page polls `GET /job/{id}` about once a second), so a slow Shop Again is many short requests. If a
   single forwarded request (including the 202) were to time out, the page can poll again with the same job id; nothing needs a long request.
d. **A React page at `/shop`** that uses `docs/api-contract.md`: a login form driven by `GET /auth/session`, then search and results, ratings and Shop Again, and the Saved panel. Product images load
   straight from the provider's https URLs (the page's CSP needs `img-src https:`).

Not verified from this repository: the Vercel rewrite, header attachment, any time limit, the cookie path behaviour through the rewrite (the cookie is `Path=/api/shop`, set by the response the browser
sees from the portfolio domain), and Fly's forwarding of the client address.

## Smoke test (needs the real services; I could not run these)

Real Postgres (Neon):
- `python -m app.shop.admin migrate` succeeds twice; `invite create`, `invite list`, `invite revoke`, `caps show` work against it.
- A search then a reload of the session: the session survives a machine restart (the first query after Neon's five-minute suspend may be slow; the retry covers it).
- Two browsers (two invites) cannot read each other's sessions; spend counters grow after a search and survive `fly machine restart`.
- `psycopg` over TLS (`sslmode=require`) connects from Fly; the `?` to `%s` placeholder conversion works for every query (the tests ran the same SQL on SQLite).

Real Fly:
- `fly deploy` builds the image; the health check on `/healthz` passes; the machine auto-stops when idle and the next request starts it (and the first request after a stop works).
- Only one machine exists (`fly scale count 1`); memory 512 MB is enough for a Shop Again (watch for out-of-memory in `fly logs`; it is a setting you can raise in `fly.toml`).
- A request without the origin secret is 403 and empty; with the wrong `Host` is 403; `GET /` is 404 JSON.

Real Vercel:
- The rewrite forwards `/api/shop/*` with the secret header; `GET /api/shop/auth/session` through the portfolio domain returns the JSON.
- Login through the portfolio sets the cookie (`Secure`, `Path=/api/shop`) and a following call is authenticated; the cookie does not appear on other portfolio paths.
- Responses are not cached (send two logins from two browsers); a slow Shop Again (about 35 s) completes through polling; the visitor address shows up as the trusted `x-forwarded-for` (rate limits per address).
