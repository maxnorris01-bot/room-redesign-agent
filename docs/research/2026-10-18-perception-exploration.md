# Perception exploration on one example photo (2026-10-18)

An informal, one-sitting exploration done in a Cowork chat while Claude Code built the spike skeleton. It is not a measurement and it made no calls with this project's API key. Its purpose: learn what each way of understanding a room photo can and cannot do before the perception schema, the canvas and the demo are designed around it.

## What was run

One staged, listing-style living room photo (2444 x 1560, analysed at 1000 x 638). It has a fireplace, a window wall with a skylight strip, wall art, two rugs, a sofa, an armchair, nested coffee tables, a glass desk and chair, plants. No pet is in the frame. Everything ran on a CPU in the Cowork sandbox (torch 2.14.1 CPU, transformers 5.20.1, OpenCV 5.0), not on Fly, so timings are indicative only.

1. **Claude direct vision.** Claude read the photo and wrote the structured JSON the perception prompt asks for (structure, movable furniture, decor, living things, style, plan-view estimate, uncertainties). Done in chat, billed to the subscription, not measured.
2. **Classical CV.** Canny edges plus probabilistic Hough lines (41 segments); k-means in LAB colour plus position (K=10) with brightness / saturation / hue rules for wall, floor, window, object.
3. **Semantic segmentation.** SegFormer fine-tuned on ADE20K, two sizes: B0 (3.8M params, about 2.9 s load, 0.4 s inference) and B5 (84.7M params, 640 px model, 1 to 4 s load, 3.7 to 4.8 s inference). B5 weights are about 340 MB.
4. **Plan-view projection.** Floor pixels and wall-base contact points projected to a top-down plan with an assumed camera: 90 degree horizontal field of view, horizon at y=288 (from vanishing lines; a level camera would be 319), camera height set so the sofa's front edge is 7 ft (gave 4.13 ft; at an assumed 75 degrees the same calibration gave about 3.4 ft, which is less plausible).
5. **Furniture removal.** Masks from B5 for furniture, rugs, art, plants and decor (33% of pixels), plus four hand-drawn boxes for things B5 missed (glass desk, floor lamp, snake plant, grass planter), dilated 19 px, then filled by LaMa (big-lama, 196 MB; 2.2 s load, 6.9 s inpaint at 1000 x 638). OpenCV Telea fill (1.2 s) as the baseline.

## What was learned

| Question | Result |
|---|---|
| Can classical CV recover room structure? | No. White walls and a white ceiling give no wall-ceiling or wall-wall edges; it confused the light rug with the wall and lost shadowed floor. Rejected. |
| Can a segmentation model label the scene? | Yes, well. B5 separated wall, ceiling, floor, window glass, rug, painting, clock, fireplace, sofa, armchair, cushions, coffee table; B0 was close on the large classes. Errors: lower armchair labelled sofa, ragged ceiling/wall edge over the window wall, missed the wall vent and hall opening, glass desk reduced to a sliver, a recessed ceiling light labelled lamp. |
| What does Claude's direct read add? | Judgement a segmenter cannot make: fixed vs movable, keepable, style, the hall opening and vent, what is out of frame, and an explicit uncertainties list. |
| Can the plan view (feet) be recovered from one photo? | Not reliably. Results depend on lens, horizon and camera height, none known. The recovered wall bases ran about 40 to 45 degrees to the camera axis and the window wall and fireplace wall came out about 9 ft apart, which is implausible. The likeliest reading is that the photo was taken from a corner, so a "frontal" plan is wrong. The right wall was hidden behind the sofa. No closed room could be built. |
| Can furniture be removed photo-realistically? | Roughly, and for free. LaMa gave a recognisable empty room (fireplace, window wall, skylight, floor kept) but with smeared gradients on large filled walls, a smudge and a bird-like artefact in the window glass, a dark floor patch, and a small leftover shape. Rated "pretty bad" by the project owner on review. OpenCV fill was worse. |
| Is putting new furniture back in feasible with these tools? | Not tested. It needs scale, perspective, lighting and occlusion handling, which is the part that would require an image-generation vendor. |

## Implications

- **Perception labels are the reliable product.** Categories, fixed vs movable, keepable, style and uncertainties are what retrieval and the self-check need.
- **Plan-view positions and room dimensions are guesses.** Schema v1 (`src/app/schemas.py`, `prompts/perception.md`) makes a plan-view position a required field for every item and defines "north" as the wall farthest from the camera, which assumes a frontal shot. Neither held up here. A canvas must be a proposal the user confirms or corrects (two or three dimensions), not a measurement.
- **Candidate schema v2 changes** (not made; v1 stays the baseline for the exploratory live run): image-space bounding box per item (checkable and needed for any compositing), a camera view descriptor (frontal / corner / oblique with a rough angle), per-item position confidence, permission to say "unknown", and a list of unseen regions. Extra fields add output tokens, so cost the change in the spike.
- **Segmentation is optional enrichment**, not a default. It would add exact floor and wall regions but means shipping torch plus weights on Fly; that cost is unmeasured.
- **Photo-real empty rooms**: removal is plausible with open models; quality and the put-back step are the open problems. This is the gated experiment in ADR 0003 and it is still gated.
- **Evaluation**: a room that is tape-measured (width, depth, a known furniture length) would give the harness a rare ground-truth case for plan-view error.

## What this does not show

- One photo, one room, one lighting condition, no pet, no poor-light case.
- Claude's boxes in the direct-vision pass were written after the B0 overlay had been seen, so some are partly copied and overstate how well Claude localises unaided. A fair test needs a blind pass on a new photo.
- The projection used guessed intrinsics. A chart title said 4.4 ft camera height; the computed value was 4.13 ft.
- No API cost or latency for Claude perception was measured; that is what the spike is for.
- Timings are from a sandbox CPU, not Fly.

## Not committed, on purpose

The photo looks like a real-estate listing image, so its rights are not ours: it must not enter this repo or any public demo. The derived overlays and canvases are not committed for the same reason. The exploration scripts live only in the Cowork sandbox because they need torch and would fall under this repo's lint and mypy; the parameters above are enough to reproduce them.

## Open questions

1. Amend ADR 0003 so the canvas is "proposed from perception, confirmed by the user"? (Pending the project owner.)
2. Does photo-real empty room move into v1 scope? Currently no: it is a gated experiment.
3. Schema v2 fields: decide after the exploratory live run shows what real perception output looks like.
