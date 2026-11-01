# The house document: house.json

`house.json` is the house as data: one flat list of elements (rooms, walls with their openings, columns, beams, stairs,
boxes, roof planes) in one frame, in inches. Everything else meets here:

- `housec.py` writes it from the Starlark sources (`house.star`, `zones/`, `lib/`).
- `ir/plan.py`, the planner, checks it and decides everything it means: defaults, the rectangles of openings, the
  lights of windows. It is the only gate: a document it plans always builds.
- `build/build.rb` draws the planner's plans in SketchUp. It reads no house document itself.
- `areasc.py` plans each design alternative's document and writes the manifest SketchUp is synced from.
- The photo tools (`overlay/`) read elements to name the features a pick aims at.

Where code and this document disagree, this document decides, and the code is fixed.

## Conventions

- **Units.** Inches, as floats. Angles in degrees.
- **Frame.** `+x` and `+y` are plan axes the house chooses, usually along its main walls as the main plan sheet draws
  them; `+z` is up. `z = 0` is the finish floor of the main storey. The owner's compass words ("north", "the street
  side") are translated to axes once, in `docs/HOUSE.md`, and never used for axes elsewhere.
- **Absolute heights.** Every z is absolute, never relative to a level, so a sunken room, a step, or a wall standing
  below the floor beside it needs no special case.
- **Ids.** Lowercase letters, digits and `_`, in at least two dotted tokens, by convention
  `<zone>.<what>[.<which>]` (`kitchen.wall.back`, `kitchen.fridge`), an opening its wall's id and a name
  (`kitchen.wall.back.window`). Unique across elements and openings together. An id is how a pick, a design layer, a
  render request and a build error name a thing, so keep ids stable when an element's numbers change.
- **Left and right** of a line `a -> b` are as seen walking from `a` to `b`: left is `a -> b` turned +90 degrees. A
  wall whose `a -> b` runs counterclockwise round a room has the room on its left.
- **Tolerance.** SketchUp merges points closer than 0.001 in, so that is the planner's tolerance: values within it of
  a bound they touch snap onto it (an opening's edge onto the wall's end, a head onto a level top), two things must be
  more than it apart to count as apart, and every size (a thickness, a width, a depth, a rise) must be more than it.
- **Required fields.** Each type below names the fields it requires; the rest are optional, with the default given.

## The top level

```json
{"ir_version": 1,
 "meta": {"project": {"name": "Example cottage", "address": "..."}, "variant": "today", "generated_by": "housec"},
 "zones": {"living": {"storey": 0}, "kitchen": {"storey": 0}, "porch": {"storey": 0, "outdoor": true},
           "roof": {"storey": 9}},
 "datums": {"main_floor": 0.0, "main_ceiling": 96.0},
 "location": {"latitude": 45.0, "longitude": -122.0, "north_angle": 0.0, "city": "Town", "country": "USA",
              "tz_offset": -8},
 "elements": []}
```

- `ir_version` is 1. `meta` is free (the planner reads none of it); a view's document records its layers there.
- `zones` declares every zone an element uses: lowercase letters, digits and `_`. `storey` (a number) orders them for
  render visibility: a camera standing in a space of storey k hides the tags `zone/<z>` of every zone whose storey is
  greater than k, and a camera outside (in no space, or in a space of an `outdoor` zone) hides nothing. The roof is
  just a high storey. `outdoor` defaults to false.
- `datums` maps names to heights (required by housec, and may be empty). It is for people, for the Starlark that cites
  them and for plancheck's elevations, which draw each as a dashed line; nothing builds from it.
- `location` is optional, and so is each of its keys. A HOUSE build sets each one into SketchUp's geo-location and
  shadow settings where it differs: `latitude` (-90..90), `longitude` (-180..180), `north_angle` (degrees, 0 up to
  360: SketchUp turns north that far clockwise from `+y`), `city`, `country`, `tz_offset` (hours from UTC, -12..14).
  The time of day, daylight saving and whether shadows show are the owner's and are never set.
- An unknown key warns.

## Fields every element carries

| field | | meaning |
|---|---|---|
| `id` | required | see Conventions |
| `type` | required | `space`, `slab`, `wall`, `column`, `beam`, `stair`, `box` or `roof_plane` |
| `zone` | required | a zone the document declares |
| `src` | required | where its geometry comes from: `cad` (vector drawings), `scan` (raster drawings), `photo` (solved cameras), `owner` (the owner's word), `measured` (tape or laser), `assumed` |
| `name` | optional | a human label |
| `finish` | optional | a palette key (see Finishes); every type but `space` takes one, and each has a default |
| `tags` | optional | free strings; a design layer's elements carry `<area>/<alt>` |
| `note` | optional | free text |

Each type below lists its own fields. Any other field warns ("unknown field"), which is how a typo like `ceilng_z`
shows up; the planner ignores it.

## Types

### space

A room or an outdoor area: a floor face and, optionally, a ceiling face.

```json
{"id": "living.space", "type": "space", "zone": "living", "src": "cad",
 "outline": [[6, 6], [237.75, 6], [237.75, 129.75], [6, 129.75]], "floor_z": 0.0, "ceiling_z": 96.0,
 "floor_finish": "oak", "ceiling_finish": "drywall"}
```

- Required: `outline`, a simple polygon `[[x, y], ...]` of either orientation, usually the room's inside wall faces (a
  repeated closing point and corners on a straight run are dropped); and `floor_z`, the finish floor.
- The ceiling is `ceiling_z` (flat) or `ceiling_plane` `{"at": [x, y, z], "slope": [dz/dx, dz/dy]}`, the plane
  through `at` rising by `slope` along x and y; never both. It must stand above `floor_z` at every corner.
- `build_floor` defaults to true; `build_ceiling` to true when the space has a ceiling (true without one is refused).
  A space building neither draws nothing in SketchUp; it still tells a render which storey and zone a camera stands
  in, as an outdoor porch over its slab does.
- `floor_finish` defaults to `oak`, `ceiling_finish` to `drywall`.
- The outline's edges are hidden in SketchUp: an outline is a room's boundary, not a thing, so two spaces with no wall
  between them draw no line across the floor, and where a floor meets a wall the crease pass draws the junction.
- A floor laid over a slab stands a finish depth (a quarter inch or more) over the slab's top, or the two faces fight
  in shaded renders; where the slab's top is itself the floor, the space sets `build_floor: false`.

### slab

A horizontal prism: a porch slab, a floor structure, a soffit, a step landing.

```json
{"id": "porch.slab", "type": "slab", "zone": "porch", "src": "cad",
 "outline": [[48, -72], [180, -72], [180, 0], [48, 0]], "z_top": -14.0, "thickness": 4.0}
```

Required: `outline` as a space's, `z_top` its top, `thickness` swept down. `finish` defaults to `concrete`. A slab is
also the one free-plan prism, for anything whose plan is not a rectangle along the axes.

### wall

```json
{"id": "living.wall.front", "type": "wall", "zone": "living", "src": "cad",
 "a": [0, 3], "b": [360, 3], "thickness": 6.0, "z0": -18.0, "top_a": 104.0, "top_b": 104.0, "kind": "exterior",
 "finish_left": "drywall", "finish_right": "siding", "openings": []}
```

- Required: `a`, `b`, `thickness`, `z0`, `top_a`, `top_b` and `kind`.
- `a`, `b`: the centreline's ends; the wall occupies `thickness/2` either side.
- `z0`: the absolute base. `top_a`, `top_b`: the absolute tops at each end, straight between; both above `z0`.
- `kind`: `exterior`, `interior`, `half_wall`, `glass_wall` or `masonry`. It sets the default finish (`siding`,
  `drywall`, `drywall`, `drywall`, `brick`) and says what the wall is to a reader.
- `finish` paints the faces that are not a side: both ends, the top, the base and the openings' reveals.
  `finish_left` and `finish_right` paint the two sides and default to `finish`.
- **Corners.** Walls meeting at a corner overlap to their outer faces, with no notch. A wall meeting another mid-length
  (a T-junction) stops at that wall's near face; running it on through would draw its end on the far face.
- `openings`: a list, sorted along the wall, that may be empty (and defaults to empty).

A wall is built as one solid: its elevation profile with each opening's rectangle cut out, pushed through its
thickness. So a wall has true reveals and no seam lines across its faces, which matters for overlays against photos.

#### Openings

```json
{"id": "living.wall.front.door", "kind": "door", "at": 96.0, "width": 36.0, "sill_z": 0.0, "head_z": 80.0,
 "swing": {"hinge": "start", "side": "left"},
 "unit": {"frame": {"left": 4.5, "right": 4.5, "bottom": 9, "top": 4.5}, "glazed": true}}
```

- Required: `id`, `kind`, `at`, `width`, `sill_z` and `head_z`.
- `kind`: `window`, `door`, `cased` (an opening with trim and no leaf) or `pass` (a plain hole: no unit, no casing).
- `at`: the distance from `a` along `a -> b` to the opening's start; `width` (> 0) runs on from there. The opening
  lies within the wall, and openings do not overlap (two may meet, sharing a jamb line).
- `sill_z`, `head_z`: absolute. The sill is not below `z0`; the head is not above the wall's top over the opening.
  A door's or cased opening's sill should stand on a floor beside the wall, or on the wall's base; one that stands on
  neither warns, since it is usually a height typed against the wrong datum.
- `swing` (doors only): `hinge` is `start` (the jamb at `at`), `end` (the jamb at `at + width`) or `both` (a pair);
  `side` is `left` or `right` of `a -> b`. It is recorded for people and tools; doors are drawn closed.
- Optional `name`, `note`, `src`.
- A door or window with no `unit` is a plain hole.

#### Units

The installed frame of a window, door or cased opening: one frame solid (the frame, mullions and rails), with a glass
pane in each light where glazed, or a leaf panel in each light of an unglazed door or window.

- `frame`: a number, or `{"left", "right", "bottom", "top"}` (left is the jamb at the opening's start). A number
  frames a window on four sides and a door or cased opening on three, with no member across the threshold; the object
  form names every side for any kind (a glazed door's deep bottom rail is its `bottom`). A thick sash member is the
  frame's own width, never a rail laid over the frame. No frame (0) leaves the glass or leaf filling the opening.
- `verticals`: mullion centres, measured from the opening's start, increasing; `mullion` (> 0) their width.
- `horizontals`: absolute z of rails and transoms, increasing; `rail` (> 0) their width.
- Every mullion and rail leaves a light of positive size on both sides, inside the frame.
- `glazed`: defaults to true for a window and false for a door; a cased opening's unit is a frame alone.
- `finish` paints the frame solid (default `trim_white`); `leaf_finish` the leaf panels (default `wood_mid`), and
  warns where the unit builds none.
- `depth`: the frame's depth across the wall, the wall's thickness or 3.5 in, whichever is less, by default.
  `offset`: the frame's middle plane from the centreline, toward the left face, 0 by default; the frame stays inside
  the wall. The glass and the leaves lie in that middle plane. On a deep wall the sash rarely sits on the centreline,
  and a frame drawn there puts its jamb and light lines off the photo's.

#### Casings

`casing` on a window, door or cased opening: the boards standing proud of the wall's faces round it.

- `width`: a number (default 3.5), or `{"left", "right", "bottom", "top"}`, each 0 (no board on that side) or more.
  The number form cases a window on four sides and a door or cased opening on three.
- `proud` (default 0.75): how far the boards stand off the face. `finish` (default `trim_white`).
- `faces`: `both` (default), `left` or `right` of `a -> b`.
- A casing stays on its wall (not past an end, under the base or over the top), and two casings on one face of a wall
  do not overlap. So an opening that runs to its wall's end (where a gable wall is split at the ridge, say) takes the
  object form, 0 on that side.
- A `cased` opening naming no casing gets 3.5 in boards on both faces and three sides where the wall has room, and is
  built without trim (with a warning) where it has not.

### column

```json
{"id": "porch.column", "type": "column", "zone": "porch", "src": "cad",
 "center": [174, -66], "shape": "square", "size": [5.5, 5.5], "z0": -14.0, "z1": 84.0,
 "capital": {"size": [7.5, 7.5], "height": 3.5}}
```

Required: `center`, `shape`, `size` or `diameter`, `z0` and `z1`. `shape` `square` takes `size` [w, d] (along x, y);
`round` takes `diameter` and is drawn as a 24-sided prism whose sides read round, each side more than 0.001 in.
`z1` above `z0`. The optional `capital` occupies the top `height` of the column, so the shaft runs
from `z0` to `z1 - height`. `finish` defaults to `paint_white`.

### beam

```json
{"id": "porch.beam", "type": "beam", "zone": "porch", "src": "cad",
 "a": [174, 0], "b": [174, -72], "width": 5.5, "depth": 9.25, "soffit_z": 84.0, "kind": "structural"}
```

A box from `soffit_z` up `depth`, `width/2` either side of `a -> b`; every field shown is required. `kind` is
`structural` or `faux`. `finish` defaults to `wood_dark`.

### stair

```json
{"id": "porch.stair", "type": "stair", "zone": "porch", "src": "cad",
 "a": [90, 0], "b": [138, 0], "descend": [0.0, -1.0], "z_top": 0.0, "risers": 2, "rise": 7.0, "run": 11.0}
```

- Every field shown is required.
- `a -> b` is the top nosing line, the edge of the upper floor at `z_top`.
- `descend` is the unit vector square to `a -> b` toward the lower floor.
- `risers` risers and `risers - 1` treads: tread k (1 up to `risers - 1`) stands at `z_top - k * rise` over
  `(k - 1) * run` to `k * run` in from the nosing line; the lower floor is at `z_top - risers * rise`.
- Built as one sawtooth solid under the treads plus the top riser's face. `finish` defaults to `wood_mid`.

### box

An axis-aligned solid: casework, appliances, fixtures, soffits, masonry, trim, and placeholders for what is not yet
identified.

```json
{"id": "kitchen.base.back", "type": "box", "zone": "kitchen", "src": "cad",
 "bbox": [6, 258, 0, 150, 282, 34.5], "kind": "base_cabinet", "front": "-y", "seams": [24, 48, 72, 96, 120],
 "rails": [28.5]}
```

- Required: `bbox` and `kind`.
- `bbox`: `[x0, y0, z0, x1, y1, z1]`, each minimum below its maximum.
- `kind`: `base_cabinet`, `upper_cabinet`, `tall_cabinet` (default finish `cabinet_wood`), `counter`
  (`counter_stone`), `appliance` (`stainless`), `fixture` (`porcelain`), `soffit` (`drywall`), `masonry` and `hearth`
  (`brick`), `trim` (`trim_white`), `placeholder` (`placeholder`, loud on purpose).
- `subtype`: free (`sink`, `fridge`, `range`, `dishwasher`...).
- `front`: the face the doors are on, `+x`, `-x`, `+y` or `-y`. `seams` (positions along the front from its low end)
  and `rails` (absolute z) draw door and drawer lines on it, and need a `front`.

### roof_plane

```json
{"id": "roof.plane.front", "type": "roof_plane", "zone": "roof", "src": "cad",
 "pts": [[-12, -18, 103], [372, -18, 103], [372, 144, 184], [-12, 144, 184]], "thickness": 8.0}
```

- Required: `pts` and `thickness`.
- `pts`: the top face, a polygon within 0.01 in of one plane, simple seen from above, not vertical.
- `thickness` (> 0) is swept straight down, so the plane's sides stand plumb.
- `holes`: optional plan rectangles `[x0, y0, x1, y1]` cut straight through (a skylight), each inside the outline
  more than 0.001 in from its edges, and apart from each other. What lines the well below is other elements.
- `finish` defaults to `roof`.

## Finishes

`build/palette.json` is the one palette: the planner reads its keys, and build.rb makes one SketchUp material per key.

```json
{"drywall": {"rgb": [218, 215, 209], "note": "painted gypsum walls and ceilings, a warm white"},
 "glass":   {"rgb": [184, 199, 206], "alpha": 0.18, "note": "clear glass, drawn see-through"}}
```

- `rgb` is the finish's colour in daylight, 0..255; `alpha` (default 1) draws it see-through in the shaded style.
- An element's finish keys must be palette keys. An unknown key warns and draws as `placeholder`, a loud magenta, so
  it shows in any shaded picture.
- To add a finish, add a key; a build updates the materials in place. The edges style draws every face in the
  background colour, so finishes never reach a photo overlay.

## Plans

`ir/plan.py` plans a document: `plan_document(doc) -> Plan`, with `ok`, `elements`, `refusals` and `warnings` (each
finding `{element_id, message}`; a finding about the document as a whole names `(document)`).

- **Refusals** are what SketchUp could not draw: a wall shorter than 0.01 in, a size of 0.001 in or less, a sill above
  its head, a mullion leaving no light, an outline crossing itself, a duplicate id, an undeclared zone. One refusal per
  element, naming it (an opening's problem names its wall and says which opening), and the other elements still plan. A
  document with any refusal is never sent to SketchUp.
- **Warnings** build but are probably not meant: an unknown finish, an unknown field, a door sill on no floor, a
  sliver of wall SketchUp may not draw.
- **A planned element** is the element's own fields with every default filled in, plus what the planner resolves:
  - walls, beams and stairs carry `length`;
  - each opening carries `rect` `[d0, d1, z0, z1]`, its rectangle along the wall with edges snapped, and `swing`,
    `unit` and `casing` (null where absent);
  - a unit carries `frame` as four sides, `glazed`, `depth`, `offset`, its finishes, and `lights`, every light as
    `[d0, d1, z0, z1]`;
  - a casing carries `sides`, `proud`, `finish`, `faces` (a list) and `outer` `[d0, d1, z0, z1]`.
  Numbers are floats. Nothing in a planned element depends on where it stands in the document, so its canonical JSON
  is a content key (see The manifest).
- `write_plan(plan, ir_path, ir_sha256, out_path)` writes the plan file build.rb reads:
  `{"plan": 1, "ir": "/abs/house.json", "ir_sha256": "...", "elements": [...], "location": {...} or null,
  "zones": {...}, "warnings": [...]}`.
- `uv run python -m ir.plan house.json` prints refusals and warnings and exits 1 on any refusal; `-o plan.json` also
  writes the plan.

## The manifest

A design area is a part of the house with alternatives to today's (docs/DESIGN.md): each alternative is a SketchUp tag
`<area>/<alt>`, and each area's `today` is a tag too (`kitchen/today`). A piece of geometry shows under one or more tag
chains: lists of such tags, one per area, all of which must be on. `areasc.py` composes the areas and writes
`out/areas/manifest.json`, which `sync` draws HOUSE from:

```json
{"manifest": 1,
 "house": {"path": "/abs/house.json", "sha256": "..."},
 "areas": [{"id": "kitchen", "name": "...", "tags": ["kitchen/today", "kitchen/island"], "today": "kitchen/today"}],
 "views": {"kitchen_island": {"name": "...", "tags": ["kitchen/island"]}},
 "plans": {"today": "/abs/out/areas/plans/today.json", "kitchen-island": "/abs/..."},
 "pieces": [{"id": "kitchen.base.back", "key": "<sha256>", "plan": "today", "zone": "kitchen", "type": "box",
             "chains": [["kitchen/today"]]}],
 "unavailable": [],
 "location": {...},
 "sha256": "..."}
```

- A **piece** is one element as one plan draws it. Its `key` is the sha256 of the planned element's canonical JSON
  without the layer tags: two placements with one key are one piece of geometry, drawn once and instanced.
- **Chains** are the tag combinations a piece shows under. `[[]]` is the shell, shown always. Otherwise each chain is
  a list of tags, one per area, all of which must be on: a piece of today's house that an alternative removes shows
  under `[["kitchen/today"]]`. Between them a piece's chains are on in exactly the combinations it stands in.
- `plans` names the plan each piece is drawn from; `today` is always there. With no areas, every piece is shell and
  there is one plan.
- Pieces are listed in document order, which orders the two elements of each crease pair.

## In SketchUp

```
HOUSE                          top group, made on Untagged; a tag the owner gives it stays
  zone/<zone>                  one container per zone, tagged zone/<zone>
    <area>/<alt>               chain containers: one nested group per tag of a chain, tagged with it
      <element id>             an element group, tagged type/<type>
    <element id>               a shell element stands in its zone group itself
      body, frame, glass, ...  parts; the switchable ones tagged part/floor, part/ceiling, part/glass,
                               part/frame, part/leaf, part/trim
  intersections                creases, tagged part/intersections
    <id a> x <id b>            one group per pair of elements that meet, nested in both elements' tags
```

- **Tags nest.** SketchUp gives an entity one tag, so an element shows only while its zone tag, its chain's tags and
  its type tag are all on. The owner switches a whole storey, every ceiling, all glass or all trim with one tag. Each
  tag a build uses is filed in the tag folder named for its prefix (`zone`, `type`, `part`, an area's id), and a sync
  files a moved one back there, which `look.py` reports as a change to the owner's state.
- **Design areas.** Each alternative is a tag `<area>/<alt>` in a tag folder named for its area. A tag new to the
  model is created shown when it is its area's today and hidden otherwise; after that the owner's choice stands.
  `Extensions > House twin > <area> > <alternative>` switches an area's tags together, with a check mark on the one
  showing. With every area on its today tag, HOUSE shows today's house.
- **Records.** Each element group records `id`, `type`, `zone`, `key`, `src` and `json` (its planned element) in the
  attribute dictionary `house_twin`; a unit's frame, glass and leaf parts record `opening`. HOUSE records `ir`,
  `ir_sha256`, `manifest_sha256`, `build_sha256` (the sha256 of build.rb's own bytes), `built_at`, `areas` and
  `zones`.
- **Creases.** Every element is its own group, so where two solids meet (an inside corner, a ceiling running into a
  wall, a cabinet against a wall) SketchUp has no edge, and an edges render would miss the line. The build computes
  those lines from the built faces and draws them under `intersections`, nested in both elements' tags so a crease
  shows only while both elements do. Glass takes no part.
- **Built in place.** A sync keeps every placement HOUSE already holds with its piece's key, instances a piece another
  group holds, builds only what nobody holds, erases what the manifest no longer places, and redraws only the creases
  of pairs naming a changed element. Every entity added or erased costs time, far more with a renderer extension such
  as Enscape watching the model (CLAUDE.md, Speed), so this is what keeps a design edit to seconds. First it undoes
  what was done by hand to the groups it goes on in: a zone group, chain container or element group moved by hand is
  erased and built again, and a container that lost its tag (its tag deleted in SketchUp) is put back on it. An edit
  to build.rb rebuilds everything once; `look.py --rebuild` forces it.
- **Checks.** Every solid is compared with the volume its plan describes, and every wall's sides with the areas they
  must cover. A failure raises an error naming the element and aborts the operation, leaving the previous HOUSE as it
  was.
- **What a build never touches**: a tag the owner has set, the active tag (Untagged, `Layer0` in the Ruby API, is
  current while drawing and the owner's put back after), the camera, HOUSE's own tag, hidden and locked flags, the
  `FURNISHINGS` group (the loose furniture, `bridge/furnishings.py`) and every top-level group but its own. It sets
  the model's units to architectural inches once, when it makes the model's first HOUSE.
- **Other top groups.** A build into another top name (a fixture, a test) draws that group alone: no location, no
  report, and HOUSE stays as it was.
- A HOUSE build writes `out/build_report.json`: the stats, one row per element group (its chain, bounds and faces) and
  one per crease pair.

## Render requests

What SketchUp renders, as `overlay/` writes it and `build/render.rb` reads it:

```json
{"out": "out/renders/living-01_edges.png", "width": 1600, "height": 1067,
 "eye": [x, y, z], "target": [x, y, z], "up": [0, 0, 1], "fov_v_deg": 55.0,
 "projection": "perspective", "style": "edges", "transparent": true,
 "hide_tags": ["zone/roof"], "areas": ["kitchen/island"], "hide_leaves": ["living.wall.front.door"],
 "top": "HOUSE", "expect_ir_sha256": "...", "crop": [x0, y0, w, h]}
```

- **Required:** `out`, `width` and `height` (whole pixels, up to 16384), `eye`, `target`, `up`, `style`, and
  `fov_v_deg` for a perspective request or `view_height` for a parallel one. Other keys are optional, and keys
  render.rb does not know (a request's own bookkeeping, such as a pixel aspect) are left alone.
- **Projection.** A centred pinhole image of `width` x `height` pixels whose full vertical field of view across the
  height is `fov_v_deg` (0..180), from `eye` toward `target`; `up` need not be square to the view. render.rb leaves
  the camera's aspect ratio at the viewport's, with which SketchUp spans the field of view across the image height at
  any image shape, and refuses a camera SketchUp reports with the field of view across the width rather than write an
  image that does not match. `"projection": "parallel"` takes `view_height` (inches across the image height)
  instead, for plans and elevations.
- **Style.** `edges`: hidden line, faces in the background colour, black edges and profiles, glass left out so rooms
  behind it show. `shaded`: material colours with black edges. Never shadows, sky, ground or fog.
- `out`: relative to the project root or absolute; the client resolves it before sending.
- `transparent` (default false): the background drawn transparent.
- `hide_tags`: tags hidden while drawing; each must be a tag of the model. Every other tag, and every tag folder, is
  on, and section cuts are off.
- `areas`: of each design area's tags exactly one is on while drawing, the one named or the area's today, so a photo's
  render, which names none, always shows today's house.
- `hide_leaves`: doors drawn open, for photos that show them open: each named door's frame, glass, leaves and their
  creases are hidden, leaving the reveals. Only a door with a unit can be named.
- `top`: the top group drawn (default HOUSE); every other top-level entity, FURNISHINGS among them, is hidden.
- `expect_ir_sha256`: refuse unless the top group records this house sha.
- `crop` is applied in Python (an off-centre principal point renders as a larger centred image, then is cropped);
  render.rb echoes it.
- The render puts back the owner's camera, rendering options, shadows, tag and tag folder visibility and top-level
  visibility, inside an operation it aborts, so it leaves no undo step. Its result carries `out`, `width`, `height`,
  `focal_px`, what SketchUp took for the camera, and the top group's `ir`, `ir_sha256` and `built_at`.

## Build identity

HOUSE records the path and sha256 of the house document its plan was made from (`ir`, `ir_sha256`), the manifest's
sha256 and the sha256 of the build.rb that drew it. The record lives in the .skp, so it survives saving and follows an
undo, which `out/build_report.json` does not. `bridge/client.py built` reads it, every render result carries it, and a
render request's `expect_ir_sha256` makes SketchUp refuse to draw a camera solved on one house over another.
