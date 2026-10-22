# 0008. Freeplay mode: no budget, a coarse price tier

- **Status:** proposed
- **Date:** 2026-10-22

## Context

Budget logic is the weak part of the pipeline. In the latest live run (`--budget 800 --replace sofa
--keep desk`) a flat per-item ceiling of $436.36 left one cheap sofa (a $199.99 faux-fur boneless
couch) when the median sofa in the search was $1,519.20: the ceiling selected for the bottom of the
category, not for what the buyer wanted. Rather than tune budgets first, the first shippable mode has
no budget: results driven by style, quality and a coarse price tier. Total and per-item budget modes
return later and will reuse the pieces built here (price stats, tier bands, slot scaling).

## Decisions

**Mode.** `--budget` is optional. No `--budget` means freeplay; with it, the existing budget path is
unchanged. `--price-tier {value,mid,premium}` (default mid) applies to freeplay only and is an error
(exit 2) together with `--budget`. `--items N` (2 to `APP_MAX_ITEMS`, default 10) is the number of
pieces (slots) to shop for: 5 by default in freeplay, 3 in budget mode (the old cap). The mode, tier
and item count are in `result.json` and the run header.

**Price tiers from the category's own distribution.** Search stats gain p33 and p67 over the priced
top-25 results. Per slot: value = at or below p33, mid = above p33 and at or below p67, premium =
above p67 and at or below 3 times the median (results above that are ignored when sizing the premium
band). The rank pool is filtered to the band in code before the rank call; a band with fewer than 3
results is widened to the adjacent tier (value and premium widen toward mid, mid widens to all); if
that still finds nothing the whole non-outlier pool is used. The band used is recorded in the slot
summary, `slot_plan.tier_bands` and the trace. Rank prompt v4 and proposal prompt v3 have no budget
or ceiling wording and choose by style fit, quality signals, fit to the room and variety; the tier is
context only. v1 to v3 (rank) and v1, v2 (proposal) stay selectable with the version flags; budget
mode keeps its defaults.

*Limits of the method.* A tier is relative to what Google Shopping returned for the query, not an
absolute price: "premium" sofas are the top third of those 25 results, which a query's wording can
shift; a thin or polluted result set (accessories, wrong category) shifts the bands; the 25-result
sample is small, so the bands are noisy; the 3-times-median guard and the minimum band size are
untuned guesses; the tiers say nothing about quality, only price rank. The self-check's budget checks
report "n/a" (never fail) in freeplay; fit, style and grounded are unchanged, and revisions trigger
only on their failures. The over-budget-options path (ADR 0007) is not used.

**Item count control and the future slider.** The slot list is gaps, replacements and then accents
(ADR 0007, amended) up to N. The CLI count is the stand-in for a UI control; a later minimalist to
maximalist slider would map to N and to decor, which is out of scope here.

**Lookup economy.** A lookup is about one SerpApi search (about $0.025 at the assumed price), the
largest per-slot cost. `APP_LOOKUPS_PER_SLOT` defaults to 1 in freeplay and 2 in budget mode. With 1,
the top-ranked finalist is looked up; if that fails or yields no retailer product page the next
finalist is tried (at most 2 attempts), and every attempt is a real, capped, tabled lookup. At about
$0.0006 for the search and $0.0250 for the lookup, a slot costs about $0.026 of estimated provider
dollars when the first lookup works, against about $0.051 with two lookups; five slots are about
$0.13 instead of $0.26. Freeplay scales the step and provider-cost caps with the item count (never
below the configured values; an untuned guess), because ten slots exceed the budget-mode defaults.

**Alternates rule change.** ADR 0007 required alternates to pass the same link rules as picks, so each
needed a lookup. With one lookup per slot the other finalists are carried as *unverified alternates*:
title, seller, search price and the Google product link (`url_kind: google_panel`), shown as
"alternate (retailer link on request)". The pick itself must still be a retailer product page. Any
alternate with no link at all is dropped. Unverified alternates are display-only: they are not
proposal candidates, so the model cannot pick one. `lookup_alternate()` looks one up on demand (for the
UI; one real lookup under the same caps) and `--lookup-alternates` does it eagerly for testing
(default off).

**Shipping status.** A live check found `shipping_known_count` of 3 for items with shipping `null`:
a missing shipping field is the provider saying nothing, and its total equalled the price, which the
count treated as "known zero". Each item and store now carries `shipping_status` (`known`, `free` or
`unknown`); only a stated amount, a stated "free", or a total above the price counts as known; a
total equal to the price does not. The final list says "free shipping" or "shipping unknown".

## Alternatives considered

1. Tune the budget ceiling first (percentile ceilings, per-category budgets). Needs more data and
   keeps a hard number in front of the visitor; deferred, and the tier stats are reusable for it.
2. Absolute price tiers per category (hand-written bands). Accurate but a large table to maintain.
3. Look up every finalist (the old behaviour). Twice the lookup spend for alternates the visitor may
   never open.

## Consequences

- Freeplay runs are cheaper per slot but shop for more slots; a default five-slot run is about $0.13 of
  estimated provider dollars and a few cents of Anthropic.
- Fit stays "unknown" for nearly every item (no size source yet); a size spike is in the backlog.
- A demo needs a cap on items and on lookup spend before public use; backlog.
- Mock-verified only. Whether tier-filtered pools give better picks than a ceiling, and how often the
  first lookup fails, are unmeasured.
