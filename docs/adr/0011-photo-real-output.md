# 0011. Photo-real output, with the 2D plan as the instant first view

> Note (2026-10-24): ADR 0012 drops the 2D plan from the product, so decision 1's "2D plan as the instant first view" no longer applies; the rest stands. This ADR's status is unchanged.

- **Status:** proposed
- **Date:** 2026-10-23

## Context

ADR 0003 chose a 2D canvas for v1 and gated photo-real output behind an experiment. The owner has now
decided that photo-real output (the visitor's own room photo with the agent's picks in it) is the main
goal, with the 2D plan as the instant first view. The agent already produces what a render needs: for
each pick, a product (name, seller, price, photo URL, real size) and what it replaces, plus perception
of the room. A spike (`scripts/render_spike.py`, package `src/photoreal`, not wired into the pipeline)
compares three ways to turn that into an image. Its findings are not in this ADR yet.

## Decision (proposed)

1. **Photo-real is the main output; the 2D plan is the instant first view.** Progressive display:
   list and plan at about 35 s (ADR 0004's full-run target), the render swapped in when ready.
2. **Reference-based placement is the chosen family**: the model gets the room photo, the product
   images and an instruction with the real sizes and the places, and edits the photo. Chosen over:
   - *Whole-image restyle with depth ControlNet* (RoomGPT and Interior AI style): cannot honour
     keep-or-replace, and the furniture it draws is not a real product.
   - *3D or AR* (IKEA, Wayfair): needs a 3D asset per product, which we do not have.
   See `docs/research/2026-10-22-room-redesign-tools.md`.
3. **Renders are labelled approximate**, and the real product photos are shown beside them. A render
   is an illustration, not a promise.

## Consequences

- **ADR 0004 (cost and latency):** a render adds seconds to minutes and dollars per run that the
  current targets do not include (the spike's gpt estimates are 30 s to several minutes per call and
  cents to a quarter-dollar per image; Gemini's list price is $0.134 per image). Either the render
  stays outside the 35 s full-run target (progressive display) with its own cost line, or the
  targets change in a new ADR before the measured run. This ADR does not change ADR 0004.
- **Fidelity caveat.** The model may restyle the product, change its proportions or alter the rest of
  the room. The spike records two cheap signals (change outside the edit region; a Haiku look-alike
  score), neither of which is a quality gate.
- **Chained edits.** Some providers take few reference images, so several swaps may be a chain of
  calls, each editing the previous result; errors compound.

## Open risks

- **Terms.** Using retailer product images (and Google-hosted thumbnails) as model inputs, and showing
  them in a public page, needs a terms review before anything ships.
- **Scale is inferred.** The instruction states real sizes, but the model decides how large a product
  looks; low room-dimension confidence makes that worse.
- **Masks are soft.** A mask is only a guide for the first image in OpenAI's API, so "change nothing
  else" is requested, not guaranteed.
- **Provider facts not verified.** OpenAI's image model id, price and input-image limit, and Decor8's
  remove-objects and decor_items details could not be verified against official docs (see the session
  doc); the spike refuses live runs for them until the owner supplies the missing values.
