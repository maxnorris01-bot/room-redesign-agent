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
