# Room redesign tools: how existing products work (2026-10-22)

**Source quality: vendor and competitor pages and press coverage, read through web search on
2026-10-23. Nothing here was measured, and none of it is independent testing.** IKEA's material is from
2022 and Wayfair's from 2023, so both may have changed. Items I could not find a source for are marked.

## Family 1: whole-image restyle with a depth or edge ControlNet

RoomGPT and Interior AI style tools take one room photo, read its depth and edges (a depth ControlNet
and often a canny/edge one), and repaint materials, lighting and furnishings on top of that geometry,
so walls, windows and layout stay. The user picks a style, not specific pieces.

- **Fit for us:** poor. The tool chooses the furniture; it cannot honour "keep this armchair, replace
  that sofa with this exact product", and the output items are not purchasable products.
- Sources: [RoomGPT mirror](https://sourceforge.net/mirror/roomgpt/),
  [ComfyUI interior workflow notes](https://comfy.org/workflows/use-cases/ai-interior-design/).

## Family 2: reference-based placement (a product image goes into the room photo)

- **DecorViz** takes a room photo and a product image from any retailer and renders the product into
  the room with matching light, shadow and perspective; it sells a "See in Your Room" button for shops.
  [Shopify listing](https://apps.shopify.com/decorviz-storefront).
- **REimagineHome** (Styldod) keeps walls, windows and doors and furnishes the room with real products
  from named retailers, as sets at budget levels that can be swapped piece by piece.
  [Tool review](https://tooliverse.ai/tools/reimagine-home).
- **Decor8 AI** has a staging endpoint whose `decor_items` parameter carries product references; the
  official SDK documentation lists no remove-objects or furniture-replace endpoint and takes image
  URLs only. [SDK docs](https://github.com/immex-tech/decor8ai-sdk/blob/main/http/README.md).
- **Edensign** and **Roomagen (Add Piece)**: I found no usable documentation, so I do not describe
  how they work.
- **Fit for us:** this family matches the goal (specific products, keep-or-replace). Fidelity to the
  product's look and its real scale is the open question; that is what the spike tests.

## Family 3: 3D or AR

- **IKEA Kreativ / Place** (2022): scans the room (LiDAR where available, photos and a phone sweep
  otherwise), lets you delete existing furniture and place IKEA items with accurate dimensions.
  [9to5Mac](https://9to5mac.com/2022/06/22/ikea-augmented-reality-app/),
  [Apartment Therapy](https://www.apartmenttherapy.com/ikeas-app-update-removes-furniture-37097182).
- **Wayfair View in Room 3D** (2023 coverage): an AR view, plus an "Interactive Photo" mode that reads
  the room's spatial information from one photo and overlays products.
  [Wayfair](https://aboutwayfair.com/augmented-reality-with-a-purpose),
  [Chain Store Age](https://chainstoreage.com/wayfair-dives-deep-ar-3d).
- **Amazon AR View**: not researched here.
- **Fit for us:** they need a 3D model of every product, which the vendors have and we do not (our
  products are search results with a photo and a title).

## What none of the sources say

None of the sources says a tool passes the product's real dimensions to the render. The 3D tools use
dimensions from their own asset library; the image-based tools do not say how scale is decided. This is
why the spike writes the real sizes (in metres and inches) into the instruction, and why renders must be
labelled approximate.
