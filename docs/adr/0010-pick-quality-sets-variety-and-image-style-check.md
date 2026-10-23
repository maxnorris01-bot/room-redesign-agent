# 0010. Pick quality: matching sets, item-count messaging, seller variety, image style check

- **Status:** proposed
- **Date:** 2026-10-22

## Context

The latest live freeplay run (`--items 5 --replace sofa --keep desk`) worked and showed four problems
beyond sizes (ADR 0009). A warm ivory boucle sofa was proposed next to a kept pale blue leather armchair
that perception had noted as "matches sofa", so the room would clash. The visitor asked for five items
and got three: only four slots opened, and the proposal left the console table empty as a duplicate of
the media unit after that slot had already paid for a search, rank and lookup. The style check passed
everything because it read only titles, often rewarding the word "Minimalist". And Article supplied
three of four picks and every alternate.

## Decisions

### Matching sets

Perception furniture items gain an optional `matches_id` (the id of another item visibly part of the
same set). It is an additive optional field, so `schema_version` stays 1 (the repo's convention is to
bump it for breaking changes) and older results still parse. Perception prompt v3 asks for it and is the
default; v1 and v2 stay selectable. Without it, a note that says an item "matches" or is "matching"
another, by id or category, is read as a link and marked as inferred.

When an item being replaced is in a set with a kept item, the run warns ("your armchair (f2) is a
matching piece of the sofa you are replacing and may clash with the new one; add --replace armchair to
replace it too", with "(inferred from the room notes)" when inferred). `--replace-matching` (off by
default) turns such kept items into replacement requests, unless the visitor explicitly kept them. The
set information also goes to the proposal as data (a "Set note" line) so its summary acknowledges it.

### Item-count messaging and families

When fewer pieces are delivered than asked for, the run prints a plain message: the requested count, the
standard categories for the room type, which are covered by furniture that stays (named), how many were
skipped as duplicates of another pick, how many were found, the slots that found nothing, and a hint to
mark items for replacement. The same data is in `result.json` (`item_count_report`).

Category families group interchangeable pieces (`categories.FAMILIES`: storage = media unit, console
table, sideboard, cabinet, filing cabinet; occasional tables = coffee and side table; chairs; extra
seating = ottoman, bench; lighting; bedroom storage). The slot planner takes at most one slot per family
unless the visitor explicitly requested a replacement in it. A skipped slot is recorded with its reason
and the next uncovered category fills the gap, so the delivered count stays as high as it can. The
families are deliberately narrow (a sofa and an armchair are different purchases); this lowers the most
a room type can deliver (living 8, bedroom 9, office 7, dining 8), which ADR 0007's earlier "10 per room
type" target no longer reaches. Pieces are never padded with duplicates.

### Seller variety

In freeplay, the pool passed to the rank stage keeps the tier band and then caps any one seller at 3 results
(`SELLER_CAP`, untuned) in search-rank order; the number removed is recorded in the slot's tier band. Rank
prompt v5 (v1 to v4 stay selectable; v5 is the freeplay default, budget mode keeps v3) asks for variety
within the finalists when quality is similar. Seller concentration (picks per seller, the largest share)
is reported in `result.json` and as a one-line footer in the final list.

*Deliberate non-goal: no cross-slot seller cap.* Slots run concurrently and each looks up only its top
finalist, so balancing sellers across slots would need a barrier before the lookups (all ranks finish,
then a global assignment, then lookups), adding to the critical path and coupling slots. The report makes
concentration visible; the cap and prompt reduce it within a slot.

### Image-based style check

Both providers carry a thumbnail URL (Serper `imageUrl`, SerpApi `thumbnail`, an https gstatic URL); it
is carried on the candidate as `thumbnail_url`. Right after a slot's rank step the top-ranked finalist's
thumbnail is fetched in a worker thread, in parallel with the lookup, so it adds nothing to the critical
path. Style check prompt v2 (v1 stays selectable and text-only; v2 is the default in both modes) is one
Haiku call that receives the prepared room photo, the style goal, the perception style descriptors, the
kept items with their colours from the perception notes, any set note, and up to 10 product thumbnails
with titles. For each pick it returns `match` (good, ok, poor), a note, and `clash` with kept items
(none, mild, strong) naming the kept item. The code records `basis` (image or text) per item. A strong
clash or a poor match fails the check: it is a warning in the final list and can trigger the existing
single revision, with the same limits as before. Cost: one Haiku call that carries a few small images,
well under a cent more (images are shrunk to at most 256 px).

**Thumbnail fetch rules (security).** The URL comes from a third-party response and this code may later run on
a server, so: https only, including every redirect (followed by hand, at most 3); the host must resolve
only to public addresses (private, loopback, link-local, reserved, multicast and unspecified addresses
and IP-literal hosts in those ranges are refused); a hard 3 second deadline and at most 1 MB; the content
type must be an image (SVG refused) and the bytes must decode with Pillow; the image is re-encoded small,
which strips metadata; the query string is never logged or traced, only the host and a reason. Any
failure falls back to the text-only check for that item and is counted in the trace. Known gap: DNS is
resolved for the check and again for the connection, so a rebinding host could still slip through; a
server deployment should also restrict egress at the network level. Mock mode never touches the network
(a generated local image serves any URL), and no test does.

## Alternatives considered

1. Replace the whole set automatically: rejected, a surprise purchase; the default is a warning.
2. A global seller cap across slots: see the non-goal above.
3. Let the model see full-size product images: more cost and a larger attack surface for little gain.
4. Fetch thumbnails for every finalist: only the top-ranked one is fetched, to keep the cost and the
   fetch surface small; an item whose pick was the fallback finalist is judged on its title.

## Consequences

- Fewer clashes get through unremarked, at the cost of one warning line per kept set member.
- The delivered count is explained, not hidden, but families lower the possible maximum per room type.
- The style call now includes images, so it is slower and slightly dearer; if the fetch fails it quietly
  becomes the old check.
- Not measured live: whether the image check is any better than the title check on real products,
  how often thumbnails fail to fetch, how well perception fills `matches_id`, and the effect of the
  seller cap on pick quality.
