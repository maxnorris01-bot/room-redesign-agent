# 0016. Clarifying questions, round 2: option pictures that are correct

- **Status:** proposed (amends the picture steps of ADR 0015; still an offline spike)
- **Date:** 2026-11-05

## Context

Round 1 of the spike measured picture COVERAGE (91% of options had a title-matched thumbnail), not CORRECTNESS. Reading the contact sheet, Max found two wrong pictures: for a dresser's
"Black or gray" a light oak dresser (the title's "black" was a metal pull), and for a nightstand's "Metal or steel" a wood nightstand with a black metal handle (the title said metal for the
handle; there may be no real metal nightstand among the first 40 listings). A wrong picture is worse than none.

## Decision

1. **Strict title matching** (`app.shop.clarify.match_rank`, used by `pick_option_images` and for the model's own evidence pointers). A value word counts only when it describes the product's
   main body. It does not count after a lead word within four tokens (`with`, `and`, `w/`, `&`, `plus`, `including`, `featuring`), nor when a hardware word follows within two tokens (`handle(s)`,
   `pull(s)`, `knob(s)`, `hardware`, `leg(s)`, `base`, `trim`, `accent(s)`, `feet`, `foot`, `hinge(s)`), nor, for a colour question, when `top` follows. The lists are module constants
   (`SECONDARY_LEADS`, `HARDWARE_WORDS`, `TOP_WORDS`, the windows beside them). A match in the first six words, or just before the product noun, is preferred; any other strict match is acceptable but
   ranks lower. An option is matched by its `value` and by each part of its label split on "or", "and", "/" and ",".
2. **Vision verification** (`app.shop.clarify_verify.verify_and_prune`, prompt `shop_clarify_verify` v1). Each option's best candidate (up to three per option, best first) is fetched through the
   guarded thumbnail fetcher and shown to Haiku vision with one forced tool call (thinking off): "is the product's MAIN BODY, not handles, legs, accents or the background, <value>?", answer yes, no
   or unclear. Only `yes` is kept; a `no` or `unclear` tries the next candidate (three rounds at most), then the option falls back to text only. Calls run in parallel: batched (up to 12 pictures a
   call, as the photo tagger) or one picture a call (`CLARIFY_VERIFY_MODE=single`); the batched mode is the default because it has fewer calls and the same pictures. A failed call or fetch is
   `unclear`, never `no`.
3. **Pruning.** An option is dropped when no listing's title strictly shows it, or when every picture tried was a clear `no` and no other title evidence is left. An option with title evidence whose
   pictures were `unclear`, unusable or absent stays as a text-only option. A question left with fewer than two options is dropped. "No preference" is the page's own chip, never an option.
4. **Independent audit** (`audit_pictures`, prompt `shop_clarify_audit` v1). A differently worded prompt is told nothing about the option and names the main colour, material and shape of every shown
   picture; code compares those words with the option (the vocabulary's own normalization, or a shared word, for any part of the label). Style, size and feature options are not auditable. It
   is the same model family as the verifier, so it is a second opinion, not ground truth.
5. **Timing.** The report gives time to text (parse, fetch, questions), the added latency of verification (image fetches and vision calls) and time to pictures; and two variants of time to text:
   the parse and the fetch at the same time (the fetch starts from the raw request with the price words stripped; the parsed query is searched again only if it differs materially, word overlap
   under 0.7), and that plus a question call shown 25 listings instead of 40. Both run in real threads against saved Serper responses; the totals use each call's live latency.
6. **The round 2 live run** is Anthropic only: hard stop $0.15, Serper from saved responses only (0 new credits: a call that was never saved skips that step, not the run). The with/without-answers
   arms re-run on the questions saved in the newest round 1 live results, so the pipeline's Serper calls are the ones already in the cache.

## Consequences

Pictures cost a few vision calls and some added seconds, and some options become text only or disappear; the report says how many and why, and measures accuracy independently. Targets: independent
picture accuracy at least 90% of shown pictures, verified coverage at least 60% of options, added verification latency (median) at most 3 s, at least 80% of questions surviving, and time to questions
(median at most 4 s, max at most 6 s) for the best variant. If they are met, the next step is the API route and page; the lift conclusion of round 1 does not depend on pictures.
