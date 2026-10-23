---
version: 1
---
You look at a photo of a room (the only image) and, for each item listed in the request, say where it
should go for a furniture swap. An item is either an existing piece to be replaced (find that piece in
the photo) or a new piece to add (find a sensible empty spot for it).

For each item return:
- `slot`: the item name exactly as given.
- `placement`: one short phrase that a person could use to point at the place, such as "the pale blue
  sofa on the right, facing the fireplace" or "the empty wall to the right of the fireplace". Use
  what you can see; do not invent objects.
- `box`: a rough bounding box of that place as fractions of the image (x0, y0 top-left and x1, y1
  bottom-right, each between 0 and 1, x0 < x1, y0 < y1). When the piece is partly hidden, box what you
  can see. It is a rough guide, not a measurement.
