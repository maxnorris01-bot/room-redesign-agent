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
