# 0014. Anonymous visitors instead of invite codes

- **Status:** proposed (amends ADR 0013, points 3 and 5)
- **Date:** 2026-10-31

## Context

ADR 0013 gated Shop Mode behind invite codes. For a portfolio demo the owner judged them more trouble than they are worth: someone has to hand codes out, revoke them and
read the list, and a visitor who is not given one sees nothing. The service stays on Fly behind the Vercel rewrite and the origin-secret check; only the way a visitor gets in changes.

## Decision

1. **Every visitor gets an automatic anonymous session.** `POST /auth/start` (no body, origin secret and Origin checks as for any write) creates a visitor (a random id, no personal
   data) and a login session, and sets the `shop_login` cookie: 128 random bits, only the SHA-256 stored, HttpOnly, SameSite=Lax, Secure per `APP_PUBLIC_URL`, `Path` = the API prefix,
   30 days, extended when used. A protected route without a valid cookie is `401 session_required`; the page calls `/auth/start` and retries. Another visitor's session is still 404.
2. **Removed:** `POST /auth/login`, invites (creation, expiry, revocation), the admin commands for them, and the code login limiter. `POST /auth/logout` stays.
3. **Limits replace invites.** The client address comes only from the trusted `x-forwarded-for` and is stored as HMAC-SHA256 under a key derived from `APP_ORIGIN_SECRET`, never raw
   (rotating the secret resets address counters). Limits, all settable by environment: $0.50 per shop session, **$1.00 per visitor address per UTC day** (scope `ip_day`), **$2.00 per
   UTC day for everyone**, 8 searches and 25 Shop Agains per session; at most 20 visitors per address and 200 in all per UTC day, and 10 starts per address per minute (429
   `rate_limited` with `Retry-After`). Clearing cookies therefore resets nothing: the daily spend follows the address recorded when the visitor was created.
4. **Retention.** Visitors, login sessions and shop sessions unused for 30 days are deleted, usage rows after 40 days: at start, then lazily from a request at most every six hours
   (the machine auto-stops, so no timer thread), and by `python -m app.shop.admin purge`.
5. **Migration 2** drops the invite tables and the test data hanging from them (invite-owned shop sessions, all login sessions, per-invite counters), renames `invite_id` to
   `visitor_id` (`ALTER TABLE ... RENAME COLUMN`, valid on SQLite and PostgreSQL) and adds `visitors`. Today's global counter is kept.
6. **A bot check is a hook, not a feature.** `auth.bot_check_allowed` is called by `/auth/start` and allows everything; Cloudflare Turnstile is a todo.
7. `cap_reached` limits are now `session`, `ip_day`, `global_day`, `searches`, `shop_agains`. The $3.00 daily default of ADR 0013 became $2.00.

## Consequences

- Anyone who can reach the portfolio can spend up to the caps; the worst day is bounded at $2.00 plus whatever the provider-side limits allow. Several people behind one address
  (an office, a phone network) share its $1.00; a visitor who changes address gets a fresh $1.00 until the global cap.
- No per-person revoke: the kill switch and the global cap are the controls.
- A bot can start up to 20 visitors from one address and 200 in all per day before being refused; Turnstile is the next layer.
- Real PostgreSQL has not run migration 2 (docs/deploy.md, "Smoke test").
