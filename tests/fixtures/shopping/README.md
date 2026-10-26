# Shopping fixtures

Trimmed copies of real provider responses (query "oak side table", gl=us), used by the mock-mode
fake providers in `app.shopping` and by the parser tests. First 8 items per search; thumbnails,
tokens and unused sections removed. No API keys or key-like strings (checked by a test).

- `serper_search_oak_side_table.json`: Serper `/shopping`. Item 6 has no `link`.
- `serpapi_search_oak_side_table.json`: SerpApi `google_shopping`.
- `serpapi_product_article.json`: SerpApi `google_product` (one store, no dimensions); gallery in
  `product_results.thumbnails` (six images), customer photos in `reviews_images`.
- `serpapi_product_article_media.json`: the same product with the gallery under `media` (six images and a video).
- `serpapi_product_ikea_guttane.json`: from a `google_immersive_product` response, which has the
  same `stores` / `about_the_product.features` keys; has Width 58 cm, Depth 39 cm, Height 12 5/8".

Mock-mode results are prefixed `[FIXTURE]` so nothing can be mistaken for live data.
