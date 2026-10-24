# Shop Mode fixtures

Synthetic, hand-shaped Serper `shopping` responses (not real listings). Titles are invented, ids and
prices are generated, `imageUrl` points at `https://mock.example/` (never fetched). Each file holds 40
results; mock mode serves them as two pages of 20. A few results have no price, a few have no rating,
and a few titles are deliberately the wrong kind of product (a sofa cover, a sofa table, a curtain
rod, a side table) so the category filter has something to remove. Titles repeat with a colour suffix
so the dedupe and the per-seller cap have something to do.
