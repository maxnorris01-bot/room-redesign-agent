"""Shop Mode (ADR 0012): a curated product search with no room photo.

A free-text request becomes a `ShopQuery`, Serper shopping results are filtered and ranked, the
visitor rates each result, and links are resolved to retailers only on demand. The room pipeline
(`app.pipeline` and friends) is not used here; it belongs to Redesign Mode.
"""
