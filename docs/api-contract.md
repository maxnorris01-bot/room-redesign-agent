# Shop Mode JSON API contract

For the React page the portfolio site will build at `/shop`. Derived from `src/app/shop/routes.py` (the route table),
`server.py`, `service.py` and what `static/app.js` calls today; a test (`tests/test_shop_deploy_contract.py`) checks that
every route in the table appears below and the reverse. The service is API only: it serves no HTML and no static files in
deployment mode. Local use (no deployment settings) has the same routes under `/api` instead of the prefix, no visit to start, and
the old page at `/`.

## Conventions

- **Base path.** `APP_API_PREFIX`, `/api/shop` by default. The portfolio's rewrite forwards `/api/shop/*` to the Fly service,
  so the browser only talks to the portfolio's own domain (same origin: no CORS headers are ever sent, none are needed).
- **Visits, not accounts.** There are no invite codes and no login form. The page calls `POST /auth/start` once (when
  `GET /auth/session` says `authenticated: false`); that makes an anonymous visitor and sets the cookie (`shop_login`,
  HttpOnly, SameSite=Lax, Secure on https, `Path=/api/shop`, 30 days, extended each time it is used). The page never reads
  it. Every route except the three `auth` routes needs it; without it: `401 session_required`, and the page calls
  `/auth/start` and retries. Clearing cookies starts a new visitor but resets nothing: limits follow the visitor's address.
- **Requests.** JSON bodies with `Content-Type: application/json` (anything else is `400 bad_request`); at most 8 KB. A
  POST must come from the portfolio origin (`Origin` or `Referer`), which a browser does by itself.
- **Responses.** JSON. Every response is `Cache-Control: no-store` and `Vary: Cookie`. Do not cache them.
- **Errors.** Always `{"error": {"code": "...", "message": "..."}}` plus extra fields where stated. `message` is a plain
  sentence safe to show; it never holds a secret or a provider detail. Codes: `session_required` 401, `forbidden` 403,
  `not_found` 404, `rate_limited` 429 (with a `Retry-After` header), `cap_reached` 429, `maintenance` 503, `bad_request`
  400 (also 405, 411, 413), `server_error` 500 (also 502 and other 5xx: the same generic sentence).
- **Sessions belong to the visitor that made them.** A session id or job id from another visitor is `404 not_found`, the same
  as an unknown one.
- **Spend caps.** Past a cap a search, Shop Again, lookup or refine is refused with
  `{"error": {"code": "cap_reached", "message": "...", "limit": "session|ip_day|global_day|searches|shop_agains", "resets_at": "2026-10-31T00:00:00+00:00" | null}}`
  (HTTP 429). `ip_day` is the allowance of the visitor's address for the UTC day, `global_day` everyone's. `resets_at` is the next
  UTC midnight, or null for a per-session limit. Everything already shown, ratings, Saved and `GET /session` keep
  working. Work already running finishes and is counted.
- **Maintenance.** With the kill switch on every route except `GET /auth/session` answers `503` with code `maintenance`.
- **Health.** `GET /healthz` (at the root, outside the prefix, no visit, no secret) returns `{"ok": true}`; it is for Fly.

## Long work: jobs and polling

A search and a Shop Again take 10 to 35 seconds. They are **not** one long request: `POST` answers `202` at once with
`{"job_id": "..."}`; poll `GET /job/{id}?session=<session id>` about once a second until `state` is `done` or `error`. A
poll is a short request, so a slow Shop Again is many short requests. At most one job runs per session, and a finished job
is forgotten after ten minutes. Jobs live in memory on the single machine: a restart loses a running job (the page shows the
error and offers a retry; session data is not lost).

## Routes

### GET /auth/session

Auth: none (this is how the page decides whether it has a visit). Response `200`:

- no visit (no cookie, or an expired or unknown one): `{"authenticated": false, "contact_email": "me@example.org" | null}`
- with a visit: `{"authenticated": true, "contact_email": string | null, "usage": {"ip_day": {"used_usd": number, "cap_usd": number}, "session": {"cap_usd": number, "max_searches": integer, "max_shop_agains": integer}, "resets_at": ISO time}}`
- under the kill switch both add `"maintenance": true`
- locally: `{"authenticated": true, "local": true}`

`usage` shows what this visitor's address has spent today and the per-session limits, nothing about other people's spend.

### POST /auth/start

Auth: none. No body. Response `200 {"ok": true}` and the `Set-Cookie`. Past a limit it is `429 rate_limited` with a `Retry-After` header
(seconds): more than 10 starts a minute from one address, more than 20 visitors a day from one address, or 200 a day from everyone (the
last two reset at UTC midnight). A later bot check can answer `403 forbidden`. Under the kill switch it is `503 maintenance`. Example:
`POST /api/shop/auth/start` with no body.

### POST /auth/logout

Auth: none. Request `{}`. Response `200 {"ok": true}`; the cookie is cleared (`Max-Age=0`) and the next visit starts a new visitor.

### GET /session

Query: `id` (optional session id; absent, malformed or unknown ids on the local server give the empty view; another visitor's id is `404`).
Response `200`: the page's whole state. Fields (the current page reads all of them):

- `mode` string ("live"), `lookups_cap` integer, `gallery` boolean, `open_timeout_s` integer, `lookup_summary` `{auto, you, median_s|null}`
- `session_id` string | null, `search` object | null (the latest search, below), `search_count` integer
- `ratings` object (result id to `{rating: "love"|"like"|"dislike"|null, comment: string}`), `saved` array (loved and liked results, newest first), `resolved` object (result id to a resolved retailer link)
- `profile` object | null (what Shop Again learned: chips in `inferred`, stated likes and dislikes, `trace`), `profile_summary` string, `can_shop_again` boolean, `shop_agains_used`, `shop_agains_cap` integers
- `topic` `{id, label, request, counted, excluded, topics}` | null, `refine` array of chips (below), `refine_usd`, `session_usd`, `prep_usd` numbers
- `photos_cached`, `photos_none` arrays of result ids, `lookup_trace` array, `lookups_used` integer, `prep` object
- `search` has: `index`, `topic`, `topic_notice`, `request`, `query` (the parsed request: `search_text`, `category`, `price_min`, `price_max`, `style_terms`, `material_terms`, `must_have`, `avoid`, `color_terms`, `shape_terms`, `context`, ...), `created_at`, `results` (array of results), `seconds`, `kind` ("search" | "shop_again"), `note`, `headline` (the one sentence above the grid), `warnings` array of strings, `queries`, `trace`, `tagging`, `serper_calls`, `serper_credits`, `anthropic_usd`, `provider_usd`, `total_usd`, `counts`, `ranked`, `alignment`, `diversity`, `seller_share`
- a result: `id`, `title`, `price` number | null, `currency`, `seller`, `thumbnail_url` (an https URL on the provider's CDN, or ""), `link` (a Google product link until resolved), `link_kind` ("google_panel" | "retailer" | "none"), `size_hint` string | null, `product_id`, `rank`, `pool_position`, `rank_reason`, `rating` number | null, `rating_count` integer | null, `also_at` array of `{seller, price}`, `source` `{kind, query}` | null

The current page displays: the cards (`title`, `price`, `seller`, `thumbnail_url`, `rating`, `size_hint`, `also_at`), the request chips from `query`, `headline` and `warnings` above the grid, the ratings, `saved`, `profile` chips, the cost footer and the "How I decided" panel from `trace` (a React page may skip the panel). Example: `GET /api/shop/session?id=Xq3kP9...`.

### GET /job/{id}

Query: `session` (the session id the job belongs to; "" for a first search). Response `200`:

- running: `{"state": "running", "phase": string, "kind": "search"|"shop_again", "steps": [{"id","label","state": "pending"|"active"|"done","done_count"|null,"total"|null}]}`
- done: the same plus `"elapsed_s": number` and `"result"`: the same body as `GET /session` (for a first search it holds the new `session_id`)
- failed: `{"state": "error", "status": integer, "error": string, ...}` (the job's own HTTP status and a safe sentence; it is `200` for the poll itself)

An unknown, expired or another visitor's job is `404`. Example: `GET /api/shop/job/Qm4c...?session=Xq3kP9...`.

### POST /search

Request `{"session_id": string | null, "request": string (1 to 300 characters)}`. Starts a job. Response `202 {"job_id": string}`. A new search with
`session_id` null makes a new session (read its id from the finished job's `result.session_id`). Errors: `400 bad_request` (empty or too long
request), `404`, "Something is already running for this session" (`400 bad_request`), `429 cap_reached` (`searches`, `session`, `ip_day`, `global_day`).
Example: `{"session_id": null, "request": "modern couches under $1000"}`.

### POST /shop-again

Request `{"session_id": string, "refine_text": string | null}`. A refine instruction typed with the click is applied first. Response `202 {"job_id": string}`.
Needs at least one rated result (`400 "Rate a few results first"`); `429 cap_reached` with `shop_agains` after the per-session count.
Example: `{"session_id": "Xq3kP9...", "refine_text": "no Walmart"}`.

### POST /rate

Request `{"session_id": string, "result_id": string, "rating": "love"|"like"|"dislike"|null, "comment": string (at most 200 characters)}`; send
`rating` or `comment` or both (a missing field is unchanged, `rating: null` clears it). Response `200 {"result_id": string, "rating": string | null, "comment": string}`.
Never refused by a spend cap. Example: `{"session_id": "Xq3kP9...", "result_id": "4100000000000102960", "rating": "love"}`.

### POST /resolve

Request `{"session_id": string, "result_id": string}`. Looks up the retailer link (a paid lookup, at most `lookups_cap` per session; a link already
resolved is free and never refused). Response `200 {"rating": {"rating": number|null, "rating_count": integer|null}, "resolved": {"result_id","status": "resolved"|"fallback","link","link_kind","seller","price","shipping","shipping_status","total","note","cost_usd","resolved_at"}, "lookups_used": integer, "lookups_cap": integer}`.

### POST /photos

Request `{"session_id": string, "result_id": string}`. More photos of one result (the product lookup, shared with resolve). Response `200 {"images": [https URL, ...], "message": string, "unavailable": boolean, "image_fields": object, "fresh": boolean, "lookups_used": integer, "rating": {...}}`.

### POST /refine

Request `{"session_id": string, "text": string (at most 200 characters)}`: one plain instruction ("focus on cream", "no Walmart", "under $400"). Response `200 {"refine": [chip, ...], "notices": [string], "refine_usd": number}`; a chip is `{"id","kind","target","strength","label","approximate": boolean,"text","note_mode"}`. `400` when nothing could be read.

### POST /refine/remove

Request `{"session_id": string, "id": string}`. Response `200 {"refine": [chip, ...]}`. `404` for an unknown chip.

### POST /preference/remove

Request `{"session_id": string, "kind": "like"|"dislike"|"inferred"|"guess", "phrase": string}`. Deletes one learned or stated preference chip. Response `200 {"profile": object, "profile_summary": string, "removed": string}`. `404` "no such preference".

### POST /preference/pin

Request `{"session_id": string, "phrase": string}`. Pins or unpins the seller or retailer chip named by `phrase` (a pin raises that seller's ceiling; it still has to be earned). Response `200 {"profile": object, "profile_summary": string, "pinned": boolean}`.

### POST /context/remove

Request `{"session_id": string, "kind": "context"|"suggested"|"avoid"|"color_term"|"material"|"shape_term", "value": string}`. Deletes one chip of the parsed request (and its quota). Response `200 {"query": object, "removed": string}`.

### POST /reset

Request `{"session_id": string}`. Start over: stops background preparation for the session. Response `200 {"ok": true}`. The page then forgets the session id and starts a new one with the next search.

## Thumbnails

There is no thumbnail route. Result cards use `thumbnail_url` directly (an https image on the provider's CDN); the service itself fetches thumbnails
only for the style check, behind its address guard, and never serves them. A React page therefore needs `img-src https:` in its Content-Security-Policy
and should send `referrerpolicy="no-referrer"` on the images.

## What the page needs for a visit

On load: `GET /auth/session`. If `authenticated` is false, `POST /auth/start` (no body), then continue; use the `usage` block for a
"remaining allowance" line. A `401 session_required` on any other route means the cookie expired or was cleared: call `/auth/start`
once and retry the request. A `429 rate_limited` from `/auth/start` carries `Retry-After`: show its `message` and wait.
