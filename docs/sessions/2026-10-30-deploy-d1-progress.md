# Stage D1: Shop Mode as a deployable invite-only JSON API, progress

| Phase | State |
|-------|-------|
| 1 Deployment mode, API prefix, proxy trust | finished |
| 2 Hosted session store | finished |
| 3 Invite codes and login | finished |
| 4 Spend caps | finished |
| 5 Outbound safety | finished |
| 6 Container and runbook | finished |
| 7 API contract, docs, report | finished |

Precondition: main is at 822633c (PR #34), read from `.git/refs/heads/main`; no git, fly or docker command run.

## Phase 1
- `app/shop/deploy.py` (`DeploySettings`, `from_env`): `APP_DEPLOY_MODE` (off by default), `APP_HOST` / `APP_PORT`, `APP_API_PREFIX` (`/api/shop` in deployment mode, always `/api` locally),
  `APP_ALLOWED_HOSTS`, `APP_ALLOWED_ORIGINS`, `APP_ORIGIN_SECRET`, `APP_ALLOW_NO_SECRET`, `APP_PUBLIC_URL` (Secure cookie when https), `APP_CONTACT_EMAIL`, `APP_SHOP_DISABLED`, `DATABASE_URL`,
  the cap settings. `problems()` lists what stops deployment mode from starting (no secret unless `APP_ALLOW_NO_SECRET=1`, bad prefix, no hosts, no origins, no database).
- `app/shop/routes.py`: the one route table (method, relative path, auth, body, name); `server.py` dispatches from it in both modes. Existing routes keep their method, request and
  response bodies; locally they are `/api` + path, in deployment mode prefix + path. New routes: `POST /auth/login`, `POST /auth/logout` (deployment only), `GET /auth/session`
  (locally `{"authenticated": true, "local": true}`).
- `server.ShopApp`: local mode unchanged (page, static files, local Host/Origin rules; the same JSON `{"error": message}`). Deployment mode: `GET /healthz` (no login, no secret, only
  `{"ok": true}`); everything else needs the origin secret (`hmac.compare_digest`, else 403 and `{}`), then a configured Host (or the portfolio's forwarded host), then a path under the prefix
  (404 JSON otherwise: no HTML, no `app.js`, no CSS, no favicon), an allowed Origin or Referer for POST/PUT/PATCH/DELETE, `application/json` for body routes, then login. Forwarded headers
  count only after the secret passed (`client_ip`), otherwise the caller is "unknown". No CORS headers anywhere. Errors are `{"error": {"code", "message", ...extra}}` (`ApiError` gained
  `code` and `extra`); 5xx messages are replaced by a generic sentence (no provider text). Every response is `Cache-Control: no-store` and `Vary: Cookie`; CSP and the other security headers stay.
  The cookie is HttpOnly, SameSite=Lax, Secure when `APP_PUBLIC_URL` is https, Path = the prefix, 14 days.
- Thumbnails: the page and the future React page load product images straight from the provider's https URLs (`img-src https:`); the server only fetches thumbnails itself for the
  style check and never serves them, so **there is no thumbnail route today and none was added**, and the `private, max-age=3600` exception has nothing to apply to.
- Long work: a search and a Shop Again are background jobs (`POST` answers 202 with a `job_id`, `GET /job/<id>` is polled); the page polls every second or so, so a slow Shop Again is
  many short requests, not one long one.
- Existing tests edited: none (local mode is byte-compatible for the existing tests). New: `tests/test_shop_deploy_api.py` (23).

## Phases 2 to 7 (summary; the full account is `docs/sessions/2026-10-30-deploy-d1.md`)
- 2: `dbstore.py` (`SessionBackend`, SQLite and Postgres behind one `Database`, migrations, retries, `SqlSessionStore`).
- 3: `auth.py`, `admin.py` (invite create/list/revoke, migrate, caps show), auth routes, login limiter, session isolation (404).
- 4: `caps.py` (`CapGuard`, `Meter`), `thresholds.py` limits, `cap_reached` 429, kill switch 503, background jobs respect caps.
- 5: `thumbnails.py` pinned connection, blocked networks, same-host redirects only.
- 6: `Dockerfile`, `.dockerignore`, `fly.toml`, `docs/deploy.md`, `make deploy-check`, `make deploy-smoke`.
- 7: `docs/api-contract.md` with a route-table test, ADR 0013, browser check of the local page, final gate.
