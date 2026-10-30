# Stage 2s: a seller affinity that checks its own bet

Date: 2026-10-30. Mock mode, fixtures and saved results only; no live call, no live eval, no live rejudge, no cache filling. `evals/shop/thresholds.json` and `THRESHOLDS.md`
are unchanged (a test asserts SHA-256 `c64fe35a...d237`). Evidence: `evals/shop/results/shop-live-20261029-140403-6d1a50.json` (read-only). Precondition: main at 780df96 (PR #33),
read from `.git/refs`, no git command run. Per-phase notes: `2026-10-30-shop-seller-affinity-progress.md`.

## What changed and why
A learned seller chip gave the seller up to half a batch from a first sample of 4 rated and 2 liked items, and what the boost earned was read only inside the overall like rate, a batch late
(case-01 round 2: Wayfair 12 of 24, 8 of them disliked; case-02 round 3: Wayfair 12, 11 disliked; case-01 round 3: Target 12, 10 liked, so a boost can be right).
1. **One allowance per seller** (`app/shop/bets.py`): chip, named searches and pin share a number of total slots that starts at 4 of 24, grows by 2 for each judged boosted batch
   (3 or more rated, 50% liked), tops out at a third of the batch (half only after two batches liked at 60%; a pin raises the ceiling, not the start).
2. **The bet is checked.** Items shown through the seller's named search or above the ordinary cap are recorded per batch; with 3 or more rated and a like rate under 35% or under
   the session baseline the bet is revoked at the start of the next Shop Again: the chip, allowance and named searches go, the message "Tried more Wayfair; you did not like most of it,
   so I stopped." shows, and the seller can return only after 8 further ratings (the marketplace dismissal logic).
3. **Shown:** "How I decided" lists each bet ("Hartwell & Co: 4 slots, first try") and the evidence table has an exposure column.
4. Fixed on the way: dismissals other than the marketplace's were lost in `build_profile` and `reapply_removed`; a pin used to outlive its chip.

## Choices where the brief was silent
- "4 of a 24-item batch beyond the normal cap" cannot also fit "a proven maximum of a third": I read the allowance as total slots, never below the ordinary cap (so a first try is
  mostly the named search's candidates and the ranker's preference), 4, then 6, then 8.
- A pin raises the ceiling and does nothing without an active bet; a seller the shopper asked for by name ("more from X") keeps its half batch.
- Cumulative boosted items decide revocation; the last judged batches decide growth; a batch with fewer than 3 ratings is not judged.
- The tier chip shares only the evidence rule, not the code that gives room, so it is left alone.
- Gated by `APP_SHOP_SELLER_BETS` (off in a bare Config, on from the environment), so older tests did not move.

## Replay check (`make eval-shop-replay`, zero spend)
The saved Serper responses cover only part of the run (13 hits, 23 misses, 10 of 12 cases did not finish, because the mock model writes different queries than the live
one). Of the six batches of cases 01 and 03, five had results (case-01 r3 hit a cache miss and shows 0); their largest seller shares were 17%, 24%, 17%, 17% and 22%: no seller above its allowance. The mock persona does not turn
against a seller, so this says nothing about revocation; that is covered by the mock tests. Nothing was spent to fill the cache.

## Tests and checks
1585 tests pass (1567 before; 18 new in `tests/test_shop_stage2s_bets.py`). ruff, `ruff format --check`, `mypy --strict`, `make shop-check` and `make eval-shop-mock` pass.

## Existing tests and checks edited (a design change forced each)
- The browser check's "a pinned seller may fill more than half, never more than 80%" (replay scenario A) is now "a pinned seller stays within its allowance, never more than 80%".

## Flag before merging
- Nothing here was checked live. A first try equal to the ordinary cap may be too timid; the new constants are judgment.
- A seller needs a chip to have a bet; the tier chip can still take up to half a batch.
- A revoked seller can reappear after 8 ratings if the evidence still earns the chip (as a first-try bet).

## Next command
`make eval-shop-live EVAL_SHOP_CONFIRM=yes CASES=case-01,case-02` (records Serper responses; read the seller bets lines in How I decided and `make eval-shop-lint RESULTS=...`).
