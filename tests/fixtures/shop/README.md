# Shop Mode fixtures

Synthetic, hand-shaped Serper `shopping` responses (not real listings). Titles are invented, ids and
prices are generated, `imageUrl` points at `https://mock.example/` (never fetched). Each file holds 100
results; mock mode serves them as pages of 20, so a typed search (up to 2 pages) and a Shop Again (up to
3 pages) see different windows and a Shop Again after a search returns mostly unseen results. A few
results have no price, a few have no rating, and a few titles are deliberately the wrong kind of product
(a sofa cover, a sofa table, a curtain rod, a side table) so the category filter has something to
remove. Some titles say "boucle" or "low profile" so a mock preference like that has something to
match. All titles are unique; the colour suffix repeats the base titles.

Each file also holds 16 results from one invented premium seller, Hartwell & Co (brushed brass,
walnut, solid wood; priced from the top of the topic's range upward): four sit among the first 40
results, so a typed search can show them, and twelve sit at the end (past the 80th), which only a
search phrased with the seller's name or its materials brings up (the mock search engine in
`app.shop.providers` boosts them for such queries). A mock session that loves two of the early ones
therefore shows a seller affinity, an upper price band and more matching results in a Shop Again.

Stage 2c: the coffee-table fixture is written to show both of the owner's cases. Its four early premium
(Hartwell & Co) items are a round walnut drum, a round brushed brass drum, a rectangular wood and black
metal lift-top and a rectangular walnut with drawers ($699 to $899); the other 100 base items cycle
round, rectangular, oval, square and unspecified shapes, walnut, oak, black steel, brushed brass, marble
and glass, 12 sellers and prices from about $140 to $900. Hartwell & Co has 40 items in all (36 past the
80th), none above $900. The mock search engine puts the seller's items first for a query that names it or
its materials, and items with a named shape first for a query that names a shape.

Stage 2d: `owner_session_replay.json` is the owner's real session in miniature: the ten rated items
(2 loved, 8 liked; West Elm for 8 of them, $143.99 to $799) with the attributes the model should read
from them (`attrs`; null where a title or photo shows nothing), and 30 invented shown extras (26 with a
shape word, 24 under $440 and the rest $465 to $610 (so the upper third of the shown prices starts at
$450, as it did live), 12 from West Elm so West Elm is half of the 40 shown results). The mock
preference call reads these attributes by id (`R1` to `R10`), so a replay can run through the service
and the page with tags derived from the real attributes rather than from title keywords.

Stage 2e: `owner_session_replay_2.json` is the owner's second real session: the nine rated items (four
loved, four liked, one disliked) with the attributes a model would tag them with, and 26 shown results
that are AUTHORED to be plausible (the owner's real shown list was not recorded), so its numbers
(warm wood 50% of tagged shown finishes, wood 80% of shown materials) are a fixture's, not a measurement.
Ids are `Q1` to `Q9` and `Y1` to `Y26`; the mock preference call reads the attributes by id. The mock
candidate tagger reads only title keywords, so coverage in the replays is the keyword coverage.

Stage 2f: `owner_session_replay_3.json` is the owner's third real session: the seven rated items (three
loved, three liked, one disliked West Elm item whose title is invented) with the attributes a model would
tag them with, 41 shown extras (48 shown with the rated) and a 72-candidate `pool`, all AUTHORED to look
like the real session (Lowe's 12, Walmart sub-sellers 20, Wayfair 10, Home Depot 3, no design premium
retailer) because the real lists were not recorded. Ids are `Z1` to `Z7`, `S1` to `S41` and `P1` to `P72`.
The pool is served by `app.shop.providers.ReplayPoolSearch` (set `APP_SHOP_MOCK_POOL=owner_session_replay_3.json`
for a mock server, as `make shop-check` does for its second server); a query that begins with a retailer
name gets that retailer's synthetic catalogue, which is what the peer step finds.

Stage 2g: `owner_session_replay_4.json` is the owner's fourth real session in miniature, AUTHORED (the real
lists were not recorded): 46 rated items (2 loved, 6 liked, 38 disliked spread over CB2, Crate & Barrel,
Lowe's, Wayfair, Target and IKEA), 45 shown extras, and a 265-candidate `pool` that contains repeated
product ids, shared thumbnails, the same title at another seller's price, the same title from the same
seller under another id, seller spellings ("west elm", "Room&Board", "Joybird Furniture", "Walmart -
JerFit Life") and copies of shown and rated items, so the de-duplication has real work. Ids are `F1` to
`F46`, `E1` to `E45` and `P1` onwards. Serve its pool with `APP_SHOP_MOCK_POOL=owner_session_replay_4.json`
(a mock server only). Pool titles are as authored, with no "[FIXTURE]" prefix, so a copy of a shown item
has the same title.

Stage 2i: `tailoring_evidence.json` is synthetic (generated, not recorded): 48 rated items with the
numbers of the owner's fifth live Shop Again (15 liked, a 31% baseline; the premium tier 7 liked of 13
rated, West Elm 3 of 5, Article 2 of 4, CB2 2 of 4; marketplace and budget 1 of 11; natural wood frame 7 of
10, black frame 4 of 31) and 136 authored candidates from West Elm, Article, CB2, marketplaces and others.
Real sessions are exported with `scripts/shop_export_replay.py` (redacted, thumbnails replaced).

Stage 2k: `serper_curtains.json` has 22 of its 116 titles prefixed Navy, Dark Blue or Blue (so a dark blue room's
suggestions and avoid colours have something to separate); the mock search puts colours named in a query first.
Stage 2m: `tests/fixtures/shopping/serpapi_product_article.json` now has `rating` 4.6, `reviews` 1203 and a
`media` list (six https images and one video, which is ignored); a lookup for a product id ending in 9 still
returns nothing.
Stage 2n: that fixture now has the live shape (`product_results.thumbnails` with six https images, and
`reviews_images` with customer photos that are not the gallery); `serpapi_product_article_media.json` keeps the old
`media` shape. In mock mode `APP_SHOP_MOCK_DUPLICATES=1` adds same-product listings from eBay, Lowe's and Overstock and
`APP_SHOP_MOCK_ONE_PHOTO=1` makes every product lookup return one photo.


Stage 3: `owner_session_replay_5.json` is the owner's fifth real session, exported with
`scripts/shop_export_replay.py` from the session of the first live curtains run ("boho curtains that pair well
with a dark blue room"): 24 rated items, a 75-item candidate pool from its pool snapshots, one comment
("duplicate"), ids renumbered, thumbnails placeholders, nothing identifying. It has no authored shown extras
(the session showed only the rated items), so a replay of it starts from the 24 rated results.
