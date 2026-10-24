# Shop Mode fixtures

Synthetic, hand-shaped Serper `shopping` responses (not real listings). Titles are invented, ids and
prices are generated, `imageUrl` points at `https://mock.example/` (never fetched). Each file holds 100
results; mock mode serves them as pages of 20, so a typed search (up to 2 pages) and a Shop Again (up to
3 pages) see different windows and a Shop Again after a search returns mostly unseen results. A few
results have no price, a few have no rating, and a few titles are deliberately the wrong kind of product
(a sofa cover, a sofa table, a curtain rod, a side table) so the category filter has something to
remove. Some titles say "boucle" or "low profile" so a mock preference like that has something to
match. All titles are unique; the colour suffix repeats the base titles.
