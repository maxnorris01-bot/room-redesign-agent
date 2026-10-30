# Stage 2s: seller affinity that checks its own bet, progress

| Phase | State |
|-------|-------|
| 1 Read and report | finished |
| 2 Graduated exposure | finished |
| 3 Outcome check and revocation | finished |
| 4 Tests, replay check, docs | finished |

Precondition: main is at 780df96 (PR #33), read from `.git/refs/heads/main`; no git command run.

## Phase 1: what gives a liked seller more room, and what takes it away (read-only, no behaviour change)

### 1. The mechanisms
- **Seller chip** (`evidence.seller_entries`, called by `decide._evidence` and so by `prefs.build_profile`): per seller `seller_stats(rated)` counts every rated item
  of that seller; `toward_fails` requires `GROUP_MIN_RATED` (4) rated, `GROUP_MIN_LIKED` (2) liked, and a like rate of at least `max(GROUP_RATE_RATIO` (1.5) x the session
  baseline, `GROUP_MIN_RATE` (0.35)`)`; marketplace and budget retailers never earn one; at most `MAX_SELLER_CHIPS` (3), best like rate first.
- **Where "up to half a batch" comes from:** `again.again_context` puts every seller chip into `AgainContext.affinity_sellers` (and `profile.pinned_sellers` into
  `pinned_sellers`); `search.build_results` (`try_take`) gives an affinity seller `affinity_cap = int(SELLER_MAX_SHARE x RESULT_COUNT)` = 12 and a pinned one
  `pinned_cap = int(SELLER_PINNED_SHARE x 24)` = 19, instead of the ordinary `seller_cap` (4, relaxed to 5 to 7 in stage 2q). `thresholds.SELLER_MAX_SHARE` 0.50,
  `SELLER_PINNED_SHARE` 0.80. The ranker is also told "Seller the shopper likes" (`search.rank_user_text`, `AgainContext.seller_affinity`), and `near` prefers that
  seller's listing when collapsing duplicates (`search._collapse_near`).
- **Liked-seller named searches** (`again.seller_plan`, `SELLER_SEARCHES` 3): the sellers `seller_entries` earns, in like-rate order, "<seller> <search text>", the next page not
  yet fetched, skipping a retailer outside the affinity tiers, a deleted chip (`is_removed_in(entry.text, profile.removed)`) and a seller whose last search found nothing
  new (`session.searched_peers[...]["empty"]`). They need no chip showing, but they use the same evidence, so in practice they exist exactly while the chip does. They run
  in the first wave of `search.run_query` (`liked_seller_keys`, kind `seller`) and are never skipped by the early stop.
- **Peer searches** (`again.peer_plan`, kind `peer`): retailers similar to a liked seller or tier, only while a seller or tier chip exists; they are ordinary sellers
  (no extra room), rotated through `session.searched_peers`.
- **Pins** (`prefs.pin_seller`, `with_pins`, `StoredProfile.pinned_sellers`): the shopper toggles a pin on a seller (or tier) chip; the key list lives on the profile and is
  carried forward unchanged by `build_profile` (`pinned = list(previous.pinned_sellers)`).
- **Retailer-tier chip** (`evidence.tier_entry`, same `toward_fails` rule over a tier's items): its own allocation in `search.run_query` (`tier_limit`: up to
  `TIER_BATCH_SHARE` 0.50 of a batch, 0.80 pinned, a quota of `tier_share x 24`). It does not go through `affinity_sellers`; per seller the ordinary cap still holds.

### 2. What removes or weakens each, and when
- The chip is **stateless**: `decide` recomputes `seller_entries(rated_map(session))` at the start of every Shop Again from all current ratings. It disappears as soon as the
  overall numbers stop passing (rated 4, liked 2, rate under 1.5 x baseline or 35%), or when the shopper deletes it (`prefs.remove_entry` puts its text in `removed`, permanently;
  only the marketplace chip has a dismissal count, `profile.dismissed["marketplace"]`, back after `MARKET_REDISMISS_RATED` 8 more ratings).
- **Ratings of items shown after the boost do not enter separately.** `seller_stats` counts all of a seller's rated items together; nothing records which items were shown
  because of the boost (`ShopResult.source.kind == "seller"` marks a named-search item only; an item shown above the ordinary cap is not marked at all). So a bet is judged only
  inside the overall like rate, one batch late (the ratings of batch N are read when batch N+1 is built), and the small first sample that earned the chip (4 rated, 2 liked) is
  enough to give the seller half a batch.
- Named searches stop when the chip is no longer earned (same function) or the shopper deleted it; peers stop when no seller or tier chip is left; the tier chip follows its
  own recomputation.

### 3. Does anything continue after the chip goes? (checked on `shop-live-20261029-140403-6d1a50.json`)
- Case-01 round 1: Wayfair had 4 shown (1 love, 1 like, 2 dislike): 4 rated, 2 liked, 50% against a baseline of about 29%: the chip. Round 2: Wayfair held **12 of 24, all 12 from the
  named search** (`source.kind` seller, query "Wayfair modern couch"); the persona rated 8 dislike, 2 love, 2 like. Round 3: the Wayfair chip was gone (trace chips show Target), and
  Wayfair is no longer among the top sellers (the named search stopped with the chip): the correction came one batch late. Target (seller chip learned after round 2) then held 12,
  10 liked and 2 loved: here the boost was right.
- Case-02 round 3: Wayfair chip learned after round 2 from 4 shown (2 like, 2 dislike), held **12 of 24, 11 disliked** (12 named-search items). It was the last round.
- **Pinned slots do not go with the chip:** `pinned_sellers` is never cleaned, and `build_results` applies the pinned cap by seller key alone, so a seller pinned once keeps up to 19
  of 24 slots after its chip is gone (no saved run pins a seller; read from the code). Named searches and the half-batch share do stop with the chip.

### 4. Tier chips
`tier_entry` uses the same evidence rule but a different allocation (`tier_limit`) and no named search; the peers of a tier are ordinary sellers. So the seller allowance and
revocation below do not reach the tier chip; I leave it as it is and say so in the report.

### Does this contradict the design? No.
The mechanisms and the failure match the brief: one overall like rate decides, there is no record of what the boost itself earned, and the boost is half a batch from a four-item
sample. Interpretation to flag: "first exposure of 4 of a 24-item batch beyond the normal per-seller cap" cannot be four extra items on top of the normal 4 and then also a
proven maximum of a third (8); I read the allowance as the seller's total slots (4, 6, 8, and 12 only when earned), never below the ordinary cap, so a first try is mostly the
named search's candidates and the ranker's preference, and each success adds 2 slots.

## Phase 2: graduated exposure
- New `app/shop/bets.py` and `SellerBet` / `BetBatch` (models.py), stored on `Session.seller_bets` (reset with the topic). Constants in `thresholds.py` with reasons
  (ADR table rows added): `SELLER_FIRST_SHARE` 1/6 (4 of 24), `SELLER_GROW_STEP_SHARE` 1/12 (2 slots), `SELLER_PROVEN_MAX_SHARE` 1/3, `SELLER_TOP_MIN_RATE` 0.60,
  `SELLER_GROW_MIN_RATED` 3, `SELLER_GROW_MIN_RATE` 0.50 (Phase 3: `SELLER_REVOKE_MIN_RATED` 3, `SELLER_REVOKE_MAX_RATE` 0.35, `SELLER_REDISMISS_RATED` 8).
- `bets.allowance`: total slots = max(ordinary cap, min(4 + 2 x level, ceiling)); ceiling a third of the batch, half only when the last two judged boosted batches were
  liked at 60% or more, the pinned share for a pinned seller (a pin raises the ceiling, it does not skip the proof). Slots scale with the batch size (`seller_slots`).
- Wiring: `again.apply_bets` fills `AgainContext.seller_slots` (and drops affinity and pins for a seller without an active bet; a seller the shopper asked for by name keeps its half
  batch); `search.build_results(seller_slots=...)` uses `max(ordinary cap, slots)` for those sellers, one number for the chip, the named search and a pin;
  `search.boosted_items` marks the items shown through the seller's named search or above the ordinary cap (4); `composition.seller_bets` rows (text like "Wayfair: 4 slots,
  first try", shown, boosted now) for "How I decided", plus an "exposure" column in the evidence table (`static/app.js`).
- Gated by `APP_SHOP_SELLER_BETS` (`Config.shop_seller_bets`, off in a bare Config, on from the environment), the repo's pattern, so no older test changed.
- Tests: allowance, growth rules, one allowance per seller, trace text, switch off (Phase 3 and 4 add the rest of the 18 in `tests/test_shop_stage2s_bets.py`).

## Phase 3: outcome check and revocation
- Recording: `search.boosted_items` marks the items shown under a seller's boost (through its named search, or above the ordinary cap of 4); `bets.record_batch` stores them
  per seller on `SellerBet.batches` (`BetBatch`: the record index and the ids), in `service._do_again`, so each rating that arrives later can be matched to the boost.
- Check: `again.prepare_bets` runs first in `run_shop_again`. `bets.update_bets`: with at least `SELLER_REVOKE_MIN_RATED` (3) boosted items rated and a like rate under
  `SELLER_REVOKE_MAX_RATE` (0.35) or under the session baseline the bet is revoked at once (before the profile is built); otherwise a judged batch with 3 or more rated and a
  like rate of 50% or more adds a level, and fewer than 3 ratings change nothing. Cumulative boosted items decide a revocation, the last judged batches decide growth.
- Revocation = the existing dismissal logic: `profile.dismissed["seller:<key>"] = rated count` (like the marketplace chip), read by `decide._evidence` through
  `evidence.seller_entries(rated, excluded)` (the chip does not return) and by `again.seller_plan` (no named search); the sellers' allowance and pin are dropped, the ordinary cap
  applies; the message "Tried more <seller>; you did not like most of it, so I stopped." goes into the warnings and the Shop Again status line; the seller can earn its chip again only
  after `SELLER_REDISMISS_RATED` (8) further ratings, as a new bet at the first-try size.
- Two bugs found on the way: `prefs.build_profile` did not carry `dismissed` forward (only the marketplace path set it), and `prefs.reapply_removed` replaced the dismissals with the
  stored copy; both now keep a seller revocation made by the same Shop Again.
- Tier chips: they share only the evidence rule (`evidence.tier_entry`, `toward_fails`), not the code that gives room (`search.run_query` `tier_limit`) or named searches, so
  they are left as they are. A tier's retailers are still individually under the ordinary per-seller cap.
- A seller the shopper asked for by name ("more from X", a refine focus chip) keeps its half batch: it is the shopper's instruction, not a bet.
- Pins: a pin no longer gives a seller 80% by itself; it raises the ceiling of an active bet and does nothing for a seller without one (before, a pin outlived its chip).
- Existing edit forced by the pin change: the browser check's "a pinned seller may fill more than half, never more than 80%" is now "stays within its allowance, never more than 80%".

## Phase 4: tests, replay check, docs
- Tests: 18 in `tests/test_shop_stage2s_bets.py` (first exposure small; revocation after bad ratings, next batch at the ordinary cap, named search gone, message shown, return only after 8 more
  ratings; a persona who likes the seller grows the allowance step by step and never above its limit; ratings below the minimum change nothing; baseline rule; one allowance per seller across
  chip, named search and pin; trace and panel text; the records and constants). Suite 1585 (1567 before).
- `make eval-shop-replay` (zero spend): the cache holds 106 saved responses, but the replay found 13 hits and 23 misses (10 of 12 cases did not finish; the mock model writes different queries
  than the live one). The batches that ran: case-01 r1 17%, r2 24%, r3 none (miss); case-03 r1 17%, r2 17%, r3 22% largest seller share: none above an allowance. The mock persona never turns against a
  seller, so revocation is covered by the mock tests, not by this. Nothing was spent to fill the cache.
- Browser check (`run_seller_bets`, `seed_bets`): "Hartwell & Co: 4 slots, first try" in How I decided (and an exposure column in the evidence table), and the revocation message on a session whose boosted
  items were disliked; screenshots `90-first-try*`, `91-revoked*` in light, dark and 390 px.
- ADR 0012 amendment 2s, SPEC, todo, working notes, `2026-10-30-shop-seller-affinity.md`. The eval reports did not change, so `evals/shop/README.md` is untouched.
