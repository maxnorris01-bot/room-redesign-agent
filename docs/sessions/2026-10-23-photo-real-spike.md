# Session 2026-10-23: photo-real spike

Branch `photo-real-spike`, from `main` at the run-report PR (#12). **No live call was made**: mock and
fixtures only, `.env` not read, no network in any test. I did read vendor documentation pages on the
web to verify facts (research, not API calls).

## What was built

An experiment, kept out of the main pipeline: package `src/photoreal/`, script
`scripts/render_spike.py`, prompts `render_edit.md` (v1, an all-at-once and a single-item variant),
`render_locate.md`, `render_judge.md`, make targets `render` (mock) and `render-live`.

- **Inputs.** `--from-run spike-output/<id>` (or `latest`) reads the picks, their real sizes and what
  they replace. Sizes are re-read from the stored titles with the current parser unless the listing
  stated them, so the old run's bookcase (read as 2 m deep) is now 21 in wide by 79 in high. Product
  images come from `render-inputs/<slot>.jpg`; each is validated (type, under 20 MB, decodes), EXIF
  stripped and capped at 1536 px. The room photo is `room.jpg` from the run (or `spike-photos/`).
- **Placement.** `--place "slot=text"` wins over `--locate` (one Haiku vision call: a phrase and a
  rough box per item). `--mask boxed` builds a PNG mask (transparent where editing is allowed, same
  size and format as the room image) and needs a box per item.
- **Instruction.** Room size with its confidence, each product's real size in metres and inches, the
  place, and "change nothing else".
- **Running.** `--approach gpt|gemini|decor8`, `--mode all-at-once|one-by-one` (chained: each call edits
  the previous result), `--quality`, `--max-usd` (default 3.00). The plan (calls, models, sizes,
  quality, estimated dollars) prints first; the run refuses if the estimate exceeds the cap and stops
  before any call that would pass it. Per call it records provider, model, parameters, seconds,
  estimated and reported cost, request id, the instruction and the image names. Keys are redacted and
  never logged; URLs are logged without query strings.
- **Output.** `render-output/<run id>/`: `result.json`, `index.html` (script-free, the run report's
  style: original beside render, steps, product references, collapsible instruction), the images.
- **Signals (not gates).** Mean pixel change outside the edit regions (Pillow); `--judge` is one Haiku
  call scoring each item's look-alike 1 to 5 and whether the rest of the room changed.
- **Mock.** Placeholder images stand in for renders and for missing product images; the whole path
  and the cap logic run offline.

## Vendor facts (checked 2026-10-23 against official pages; summaries came through a fetch tool)

**Gemini, verified enough to run live:**
- Model id `gemini-3-pro-image` (stable; the brief's `gemini-3-pro-image-preview` is not listed).
- Endpoint `POST https://generativelanguage.googleapis.com/v1beta/interactions`, header
  `x-goog-api-key`, body `{"model": ..., "input": [{"type":"text",...}, {"type":"image","mime_type":...,
  "data": <base64>}]}`, image returned at `output_image.data`.
- **Reference limit: up to 6 object images for Gemini 3 Pro Image** (the brief's "2" was not
  confirmed), so a single call can carry up to 6 products; one-by-one stays the default.
- Price $0.134 per 1K/2K image, $0.0011 per input image (standard tier).
- Not documented: usage and request-id fields in the REST response (the image is read tolerantly from
  the top level or under `interaction`), an image-size option, and a mask input.

**OpenAI, NOT verified; live runs blocked until you supply two values:**
- The docs list `gpt-image-2.5-sunburst` and `gpt-image-2.5-flare`; **`gpt-image-2` is not listed.**
  Set `RENDER_OPENAI_MODEL` (or `--model`).
- No price is published on the pages I could read. Set `RENDER_OPENAI_USD_PER_IMAGE`; without it the
  plan shows an assumed gpt-image-1-era price, clearly labelled, and live is refused.
- Verified: custom sizes (multiples of 16, aspect 1:3 to 3:1, edges up to 3840), quality values,
  base64 output, a mask needs an alpha channel and must match the image's format and size (under
  50 MB), complex prompts up to about 2 minutes (the brief's 30 s to 5 min was not confirmed; the
  client timeout is 600 s). Not found: the maximum number of input images, the edits reference page
  (404), a usage field. The multipart field names (`image[]`, `mask`) follow older gpt-image docs.

**Decor8, NOT verified; stub only:**
- The official SDK HTTP docs list `/generate_designs_for_room` (with `decor_items` and `mask_info`
  as strings, format undocumented) but **no remove-objects or furniture-replace endpoint**, and
  `input_image_url` is a URL, so a local photo cannot be supplied without uploading it to a public
  host. No price. I did not build a "remove, then place" test; live is refused with these reasons.

## Decisions
- Package named `photoreal` (a module `render_spike` would clash with the script for mypy).
- Gemini may do all-at-once when 6 or fewer products (verified limit); default stays one-by-one.
- The model id and price are the blockers for gpt; other unverified items are notes.
- `--locate` and `--judge` use `APP_MODEL_STYLECHECK` (Haiku) and count in the Anthropic cost.
- `src/photoreal` and `tests/test_photoreal.py` have a ruff `E501` ignore (long prompt-ish strings).

## Status
- Tests: 537 passed (503 before; 34 new in `tests/test_photoreal.py`). Lint and `mypy --strict`
  clean; mock `make eval-fast` pass_rate 1.0.
- Not checked: any real render, the real response shapes, real prices and latency, the quality of any
  output. All cost numbers are estimates.

## How to run live (one approach at a time; the plan and estimate print first)

Put `sofa.jpg`, `media-unit.jpg`, `bookshelf.jpg` (product photos) in `render-inputs/`, and put the
provider key in your environment or `.env` yourself.

- `make render-live ARGS='--from-run spike-output/20261023T030651Z-5358fa --approach gemini --mode one-by-one --max-usd 1.00'`
- `make render-live ARGS='--from-run spike-output/20261023T030651Z-5358fa --approach gpt --quality medium --max-usd 1.00'` (after setting `RENDER_OPENAI_MODEL` and `RENDER_OPENAI_USD_PER_IMAGE`)
- Add `--locate --mask boxed` (gpt) or `--judge` as wanted. Decor8: not runnable.

Next command: `make render`, then `open render-output/<run id>/index.html`.
