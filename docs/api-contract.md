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
- `clarify_answers` (only with `APP_SHOP_CLARIFY` on): array of `{id: "answer:<question id>", question, label, dimension}`, the answers that apply to the next Shop Again, shown as chips; delete one with `POST /refine/remove`
- `search` has: `index`, `topic`, `topic_notice`, `request`, `query` (the parsed request: `search_text`, `category`, `price_min`, `price_max`, `style_terms`, `material_terms`, `must_have`, `avoid`, `color_terms`, `shape_terms`, `context`, ...), `created_at`, `results` (array of results), `seconds`, `kind` ("search" | "shop_again"), `note`, `headline` (the one sentence above the grid), `warnings` array of strings, `queries`, `trace`, `tagging`, `serper_calls`, `serper_credits`, `anthropic_usd`, `provider_usd`, `total_usd`, `counts`, `ranked`, `alignment`, `diversity`, `seller_share`
- a result: `id`, `title`, `price` number | null, `currency`, `seller`, `thumbnail_url` (an https URL on the provider's CDN, or ""), `link` (a Google product link until resolved), `link_kind` ("google_panel" | "retailer" | "none"), `size_hint` string | null, `product_id`, `rank`, `pool_position`, `rank_reason`, `rating` number | null, `rating_count` integer | null, `also_at` array of `{seller, price}`, `source` `{kind, query}` | null

**Filters (stage F1, additive).** Every result in `search.results` has `attrs`, and `search` has `facets` and `facets_unknown`. They are in `GET /session` and in a done job's `result`
(the same body), for the latest search or Shop Again; a Shop Again batch has its own. Nothing about ranking or the order of `results` changed.

```ts
type Dimension = "color" | "material" | "shape" | "style" | "size" | "feature";
// result.attrs: always all six keys; lowercase keys of clarify_vocab.json; [] = unknown for that dimension. Colour values are keys of its
// `colors` table (grey is written "gray") plus "multicolor". No price words. Sessions saved before F1 get attrs read from the title.
type Attrs = Record<Dimension, string[]>;
interface FacetOption {
  value: string;                 // an attrs value, e.g. "gray", "mid-century"
  label: string;                 // "Gray", "Mid-century"
  count: number;                 // shown results whose attrs include the value (>= 1)
  icon: string | null;           // same icon key as ClarifyOption.icon (draw it), else null
  swatches: string[] | null;     // hex colours for colour options, else null
}
interface Facet { dimension: Dimension; title: string; options: FacetOption[]; }   // title: "Colour" | "Material" | "Style" | "Shape" | "Size" | "Feature"
// search.facets: Facet[]            most useful first (the dimension that covers the most shown results; ties colour, material, style, shape, size, feature)
// search.facets_unknown: Record<Dimension, number>   shown results with no value for the dimension, all six keys always present ("3 have no colour info")
```

Facet rules: counts are over the shown results of this batch. An option needs `count >= 1`. A dimension is listed only with at least 2 options and only when some option tells results apart (not every
result carrying every option). At most 10 options per dimension, by count, then vocabulary order. `icon` and `swatches` come from `option_visual` (`clarify_visual.py`). A filter is the page's job:
`attrs` on each result is what to match against. Example (rug search): `{"color": ["gray","blue"], "material": ["wool"], "shape": ["rectangular"], "style": ["vintage"], "size": [], "feature": ["washable"]}`.
Colour comes from the photo tags the search already makes; for a shown result without one, one vision call (6 s cap in all) names the product's colours (multicolour patterns: "multicolor" plus up to two),
else the title's colour words. The other dimensions come from the listing title only (the search gives no description), never from the photo.

The current page displays: the cards (`title`, `price`, `seller`, `thumbnail_url`, `rating`, `size_hint`, `also_at`), the request chips from `query`, `headline` and `warnings` above the grid, the ratings, `saved`, `profile` chips, the cost footer and the "How I decided" panel from `trace` (a React page may skip the panel). Example: `GET /api/shop/session?id=Xq3kP9...`.

### GET /job/{id}

Query: `session` (the session id the job belongs to; "" for a first search). Response `200`:

- running: `{"state": "running", "phase": string, "kind": "search"|"shop_again", "steps": [{"id","label","state": "pending"|"active"|"done","done_count"|null,"total"|null}]}`
- done: the same plus `"elapsed_s": number` and `"result"`: the same body as `GET /session` (for a first search it holds the new `session_id`)
- failed: `{"state": "error", "status": integer, "error": string, ...}` (the job's own HTTP status and a safe sentence; it is `200` for the poll itself)

An unknown, expired or another visitor's job is `404`. Example: `GET /api/shop/job/Qm4c...?session=Xq3kP9...`.

**`clarify` (optional, stage Q1).** A search job started while `APP_SHOP_CLARIFY` is on carries one more field, `clarify`, in every state
(running, done, failed); with the flag off the field is absent and the response is exactly what it was. A Shop Again job never has it.
It is the same object `POST /job/{id}/answers` returns under `clarify`:

```ts
type ClarifyState = "pending" | "ready" | "skipped" | "failed";
interface ClarifyOption {
  label: string; value: string;
  icon: string | null;                       // a key of clarify_vocab.json ("wool", "washable", "round" ...): draw it; null if none
  swatches: string[] | null;                 // up to 3 hex colours (colour questions): draw them; null if none
  picture_url: string | null;                // https thumbnail, only for an option with neither icon nor swatches
}
interface ClarifyQuestion {
  id: string; text: string;
  kind: "choice" | "size_range";
  dimension: "color" | "material" | "shape" | "style" | "size" | "feature";
  options: ClarifyOption[];                  // 2 to 4; the page adds its own "No preference"
  picture_eligible: boolean;                 // some option has no icon/swatch and a picture may still arrive
}
interface Clarify {
  state: ClarifyState;                       // pending: being written; skipped/failed: nothing to ask
  questions: ClarifyQuestion[];              // [] unless state is "ready"; question 1 first (id q1), q2..q5 are appended later
  more_coming: boolean;                      // true while questions 2..5 are still being written; keep polling
  timing: { q1_ms: number | null; rest_ms: number | null; first_pictures_ms: number | null; source: "template" | "model" | "none" };
  pictures_state: "pending" | "done";        // text comes first; picture_url fills in on a later poll
  no_preference_value: "no_preference";      // the option_value that means "no preference"
  answers: { question_id: string; option_value: string }[];   // what the server holds so far
  submitted: boolean;                        // a submit was received
  applied: boolean;                          // the apply point passed: answers are final, no more posts
  deadline: {
    idle_grace_s: number;                    // 30: with no interaction the search ranks this long after question 1 was shown
    max_wait_s: number;                      // 60: once touched or answered, the most it waits (from when question 1 appeared)
    waiting: boolean;                        // the search is ready to rank and is holding for the answers now
    seconds_left: number | null;             // until the current deadline, only while waiting
    shown_s_ago: number | null;              // since question 1 appeared (null before)
  };
}
// GET /job/{id} response: ... existing fields ... & { clarify?: Clarify }
```

How the page should use it: poll as it does now. When `clarify.state` is `"ready"`, show `questions` (text first; swap `picture_url` in as `pictures_state`
moves to `"done"`; a `null` picture means show the option as text). Post answers as they are chosen, and `submit: true` when the shopper presses the
button. The search keeps working meanwhile; the results arrive in `result` as always. Once `applied` is true stop posting. `skipped` and `failed`
mean show nothing (the search is not held up). Picture URLs are the providers' own https thumbnails: use them as the result cards do (no proxy).

### POST /job/{id}/answers

Auth: visit cookie; the job must be the visitor's (another visitor's or an unknown job is `404`). Request:

```ts
{ answers: { question_id: string; option_value: string /* an option's value, or "no_preference" */ }[];  // at most 20; may be []
  submit?: boolean;                          // default false; true ends the wait: rank now
  touched?: boolean;                         // default false; true = the shopper is interacting: wait up to max_wait_s even with no answer yet
  session_id?: string }                      // only to match a job started with a session id (local server)
```

Response `200 { clarify: Clarify }` (the object above, after the change). Rules: a later answer to the same question replaces the earlier; a
`question_id` that is not one of this job's questions is ignored; an `option_value` that is not one of that question's options (and not
`"no_preference"`) is `400 bad_request` and nothing in the request is recorded; the same request twice changes nothing (idempotent); `409` when the job
has no open questions (`state` is not `"ready"`, a Shop Again job, or the flag is off) and when the apply point has passed (`applied` true), except that
repeating what was applied answers `200`; `429 rate_limited` after 60 answer posts to one job. Answering is not a search: it does not count toward the
8-search limit and costs nothing.
Example: `POST /api/shop/job/Qm4c.../answers` with `{"answers": [{"question_id": "q_color", "option_value": "walnut"}], "submit": true}`.

### POST /search

Request `{"session_id": string | null, "request": string (1 to 300 characters)}`. Starts a job. Response `202 {"job_id": string}`. A new search with
`session_id` null makes a new session (read its id from the finished job's `result.session_id`). Errors: `400 bad_request` (empty or too long
request), `404`, "Something is already running for this session" (`400 bad_request`), `429 cap_reached` (`searches`, `session`, `ip_day`, `global_day`).
Example: `{"session_id": null, "request": "modern couches under $1000"}`.

### POST /shop-again

Request `{"session_id": string, "refine_text": string | null}`. A refine instruction typed with the click is applied first. Response `202 {"job_id": string}`.
Needs a finished search, not a rating: with nothing rated the batch is simply new (everything already shown in the session is excluded, and the fetches reach past it: the next page and written variants of the search). `400 "Search first"` before any search; `429 cap_reached` with `shop_agains` after the per-session count. `can_shop_again` (in `GET /session`) is true whenever the session has a finished search, the visitor is under `shop_agains_cap`, and no job is running for the session. Refine filters (see `POST /refine`) apply to the batch: one extra targeted search, and the batch is ordered so results that satisfy the filters come first.
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

Request `{"session_id": string, "text": string (at most 200 characters)}` and/or `"filters"` (below): one plain instruction ("focus on cream", "no Walmart", "under $400"). Response `200 {"refine": [chip, ...], "notices": [string], "refine_usd": number}`; a chip is `{"id","kind","target","strength","label","approximate": boolean,"text","note_mode"}`. `400` when nothing could be read.

**Refine filters (refine B1, additive).** Typed preferences for the NEXT Shop Again (not a switch that hides the current results). Send them on this endpoint:

Request `{"session_id": string, "filters": [{"dimension": Dimension, "values": [string, ...]}, ...]}` (with or without `text`; with `filters` only, nothing is interpreted and no model is called). `dimension` and `values` are keys of the shared vocabulary, the same as `attrs` and `facets` (`grey` is accepted for `gray`; at most 6 values per dimension). The payload REPLACES the whole set: `[]` clears it, a dimension with no values is dropped. Response `200`, the same shape as above. `400` for an unknown dimension or value, or a non-list.

Each filter is a chip in `refine` (kind `filter`, id `filter:<dimension>`), removable with `POST /refine/remove` and applied to every following Shop Again until removed. A new product category starts a new topic and clears them. Example chip: `{"id": "filter:color", "kind": "filter", "target": "color:gray,blue", "strength": "focus", "label": "Colour: gray or blue", "approximate": false, "text": "", "note_mode": ""}`.

What a Shop Again does with filters: one targeted search (the typed search text plus at most 4 filter words, colour first; a second page only if the first added nothing new, never a third), merged with the normal candidates and de-duplicated; candidates are ordered by tier (satisfies every dimension, OR within a dimension and AND across; then a fitting colour before an unread one before a known wrong one; then the most dimensions, colour > style > material > the rest) and the batch fills from the best tier first, so the page still fills. The extra search counts in `serper_calls`, `serper_credits` and `total_usd`; it is still one Shop Again. A first search never uses filters.

`search.filters_match` (always present): `null` unless the batch was made with filters, else `{"satisfied_all": integer, "shown": integer, "filters": [Dimension, ...]}`, computed from the shown results' `attrs`: "5 of 12 match all your filters".

**Price range (additive).** A typed price for the NEXT Shop Again. Send it on the same endpoint, alongside `filters` and/or `text`:

Request `{"session_id": string, "price": {"min": number | null, "max": number | null}}`. It REPLACES the stored price; `{}` or both null clears it. Numbers are `>= 0`; `min <= max` when both are given. `400` otherwise (a non-object, a negative, a non-number, `min > max`); a `400` stores nothing, not even the `filters` sent with it. With `price` only (no `text`) no model is called. Response `200`, the same shape as above: the `refine` list gains (or loses) one chip, id `price`, kind `price`, strength `focus`, removable with `POST /refine/remove` (id `price`), cleared by a new product category like the other refinements, and applied to every following Shop Again until removed.

| range | `target` | `label` |
|---|---|---|
| min and max | `price:50-200` | `Price: $50-$200` |
| max only | `price:-200` | `Under $200` |
| min only | `price:50-` | `Over $50` |

Whole dollars read without cents ("$50"); a fractional bound reads with them ("$49.50"). Example chip: `{"id": "price", "kind": "price", "target": "price:50-200", "strength": "focus", "label": "Price: $50-$200", "approximate": false, "text": "", "note_mode": ""}`.

What a Shop Again does with a typed price (a first search, and a Shop Again without one, behave exactly as before):

1. The typed range REPLACES the budget parsed from the original request and any text refinement such as "under $400" (both ends: a parsed floor is dropped when only a max is typed).
2. Strict filter, after the fetches and the merge and before ranking: every candidate whose item price (the one the card shows, not shipping) is outside `[min, max]`, inclusive, is dropped, and so is every candidate with no usable USD price. Nothing out of range is ever shown, even if the batch ends short.
3. Fetch: when there are typed filters, the price words come first in the targeted search ("under $200", "over $50" or "$50 to $200"), then the filter words, at most 4 added words in all. Serper's shopping endpoint takes no price parameter, so there is no other lever. If the strict filter leaves fewer than a full batch (24), ONE extra page is fetched (the targeted words with the price words, the next page for them) and the result re-filtered; it counts in `serper_calls`, `serper_credits` and `total_usd`, and replaces the usual refill search. A failed extra page is a warning.
4. It works with typed filters: the price drop comes first, then the filter tiers.

`search.price_filter` (always present): `null` unless the batch was made with a typed price, else `{"min": number | null, "max": number | null, "removed": integer}`, `removed` being how many candidates the price rule dropped (out of range or unpriced) across everything fetched.

### POST /refine/remove

Request `{"session_id": string, "id": string}`. Response `200 {"refine": [chip, ...]}`. `404` for an unknown chip. An answer to a clarifying question
(stage Q1) is deleted the same way with `id` `"answer:<question id>"`: its constraint is gone from the next Shop Again, and the response also holds
`"clarify_answers": [...]`.

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

**Question 1 fast (stage B2).** Question 1 is shown within about 2 s of the search starting (instantly for a common category: a template, no model call); questions 2..5 are appended to `questions` while the shopper answers question 1 (`more_coming` is true until they are in or the call fails). The page should render icons and swatches itself and never wait for `picture_url`. Answers apply to the listings already fetched and tagged: answering causes no new search. `clarify.timing` and the server log line `clarify_timing job=<id> q1_ms=.. rest_ms=.. source=.. questions=..` (`fly logs -a room-redesign-shop | grep clarify_timing`) give the timings.
