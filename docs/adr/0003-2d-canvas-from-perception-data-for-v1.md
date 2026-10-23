# 0003. 2D canvas from perception data for v1; photo-real is a gated experiment

> Note (2026-10-23): the owner has since chosen photo-real as the main output with this 2D plan as the instant first view; see ADR 0011 (proposed). This ADR's status is unchanged.

- **Status:** accepted
- **Date:** 2026-10-18

## Context

The intended experience includes a "blank room" the new furniture is placed into, and later a manual redesign mode. Claude can describe and locate things in a photo but does not generate or inpaint images, so a photo-real empty room needs a second image-editing vendor with its own key, cost, latency and quality risk.

## Options considered

1. **2D top-down canvas rendered from structured perception output.** No second vendor, cheap, and the same data model backs the later manual editor. Not photo-real.
2. **Photo-real empty room via an image-editing model.** Most impressive visually; second vendor, higher per-run cost and latency, uncertain quality; needs its own spike.
3. **2D for v1, photo-real as a gated experiment.**

## Decision

Option 3. v1 ships the structured pipeline and a 2D canvas. One image-editing model gets tested on 3 rooms later and is adopted only if its cost, latency and quality clear targets set beforehand.

## Consequences

- Perception output is the product's core data model and must be schema-versioned.
- The manual editor (v1.1) builds on the same canvas.
- Photo-real work is not on the v1 critical path.
