# Matching photos to the model

A photo with a solved camera lets SketchUp draw the model from exactly where the photographer stood. The model's
edges laid over the photo show, line by line, where the model is right and where it is not. A solved camera also
turns "read it off the photo" into a number: a pixel back-projected onto a named plane is a model coordinate.

Drawings (`docs/DRAWINGS.md`) give the designed dimensions; photos show what was built, and check the model as a
whole. Listing photos are ideal: wide, sharp, level, and covering every room.

Everything here runs from the repository root. The camera conventions (pixel coordinates, the camera frame, the
lens) are written once, at the top of `overlay/camera.py`.

The example cottage has one photo, `living-01`: a synthetic picture of its living room drawn from the model, with its
picks (`overlay/picks/living-01.json`) and solved camera committed. The commands below run on it as written, so it is
the place to try a tool, and to read a working picks file, before the owner's photos.

## The loop for one photo

1. **Add the photo** as `sources/photos/<stem>.jpg` (or `.png`). Use a short lowercase stem naming the room and a
   number, such as `living-01`. A camera is tied to the photo's pixels, so keep the original file: never crop,
   resize or rotate it. A HEIC (iPhone) or WebP photo is converted once, at full size, before any picks are read:
   `sips -s format jpeg IMG_0412.heic --out sources/photos/living-01.jpg`. Give it a row in the photo index of
   `docs/HOUSE.md`.
2. **Look at the photo whole.** List the model features it shows sharply: room corners, window and door jambs, sills
   and heads, counter and cabinet corners, floor and ceiling lines, beam edges. Note what is furniture or otherwise
   not in the model, and check what it shows against the area's inventory in `docs/HOUSE.md`.
3. **Read pixel coordinates** (`overlay.annotate`, below).
4. **Write the picks**, `overlay/picks/<stem>.json`: each pixel and the model feature it shows.
5. **Solve:** `uv run python -m overlay.solve <stem>`. It writes `overlay/cameras/<stem>.json`,
   `overlay/requests/<stem>.json` and a working overlay of the picks, or refuses a camera the picks do not pin down,
   and says why.
6. **Render and compose:** `uv run python look.py <stem>` compiles the house, syncs SketchUp, renders the photo's
   view and writes `out/overlays/<stem>_overlay.png` and `<stem>_clean.png`. With the house already built,
   `uv run python -m overlay.render <stem> --compose` does the last two steps alone.
7. **Look at the whole overlay**, every model edge against the photo, not only near the picks. Then decide: is the
   camera off (everything shifted or scaled together), or the model (one element off while its neighbours land)?
   Fix the picks or the model, and look again.

## Reading pixel coordinates

    uv run python -m overlay.annotate living-01                      overview and zoomed tiles
    uv run python -m overlay.annotate living-01 --at 412,233 --at 870,230

It works on any image: a photo stem, or a path to a photo, a rasterised drawing or a render. Output goes to
`out/annotate/<stem>/`: without `--at`, `overview.png` (a grid every 50 px) and 256 px tiles at 3x (a grid every
10 px); with `--at`, only a crop at 8x for each point, with a 2 px grid and a crosshair exactly where the numbers you
gave land. `index.json` lists what the run wrote.

Pixel coordinates have pixel corners on integers: the grid line labelled 640 is the left edge of pixel column 640,
so a feature centred in that column reads 640.5. Read a feature on its tile to a few pixels, then confirm it with
`--at` and adjust until the crosshair sits on it; a whole photo read as an image is scaled down, so never take a
coordinate from it. Read corners where two edges meet, not where a shadow or a highlight seems to end. Where two
walls of one colour meet, the corner can be no more than a change of shade: find it where the floor or ceiling lines
along the two walls meet, or pick those lines instead.

## The picks file

Four of the 15 picks in `overlay/picks/living-01.json`, the example's:

    {"photo": "living-01.jpg",
     "notes": ["The living room from beside the kitchen opening, toward the front door and the west window"],
     "points": [
      {"label": "inside corner, front and west walls, at the floor", "px": [1118.5, 778.3], "sigma_px": 1.5,
       "ref": [{"el": "living.wall.front", "face": "left"}, {"el": "living.wall.west", "face": "+x"},
               {"el": "living.space", "level": "floor"}]},
      {"label": "floor line along the west wall", "px": [1289.6, 810.8], "sigma_px": 1.5, "free": "y",
       "ref": [{"el": "living.wall.west", "face": "+x"}, {"el": "living.space", "level": "floor"}]},
      {"label": "front window: casing's inner corner, start jamb at the head", "px": [606.6, 371.7], "sigma_px": 1.5,
       "ref": {"el": "living.wall.front.window", "jamb": "start", "level": "head", "face": "left",
               "casing": "inner"}},
      {"label": "front door: casing's inner corner, start jamb at the head", "px": [869.1, 406.5], "sigma_px": 1.5,
       "role": "check", "ref": {"el": "living.wall.front.door", "jamb": "start", "level": "head", "face": "left",
                                "casing": "inner"}}]}

and three kinds of pick it does not need: a feature a known distance from a modelled one, a feature that is not in the
model at all, and a door standing open:

      {"label": "picture rail's lower edge along the front wall", "px": [829.0, 330.0], "free": "x",
       "ref": [{"el": "living.wall.front", "face": "left"}, {"el": "living.space", "level": "ceiling"}],
       "offset": [0.0, 0.0, -12.0], "offset_note": "12 in below the ceiling, the owner says; the rail is not modelled"}
      {"label": "sofa arm, front corner", "px": [655.0, 590.0], "role": "check",
       "model": [180.0, 96.0, 25.0], "explicit_note": "furniture, not in the model; taped"}
    "open_leaves": ["living.wall.front.door"]

| field | meaning |
| --- | --- |
| `label` | unique within the file; say what the feature is |
| `px` | `[u, v]`, read off the photo as it is |
| `ref` | the model feature, as a feature reference (below), resolved from house.json every time the file is loaded |
| `free` | the axis a reference leaves unpinned, declared: the pick is a line pick, its pixel anywhere on that model line |
| `offset`, `offset_note` | move a resolved reference by `[dx, dy, dz]` inches, for an unmodelled feature a known distance from a modelled one |
| `model`, `explicit_note` | explicit `[x, y, z]` inches, only for something the model does not contain; one coordinate may be `null` (a line), or with all three a `direction` makes a line along it |
| `role` | `solve` (default) or `check`: a check is held out of the solve and measured with the solved camera |
| `snap` | refine the pick: to the nearest corner for a point, onto the nearest straight edge for a line |
| `sigma_px` | expected error in pixels; default 6 by eye, 1.5 after a successful snap |
| `note` | free text |
| `notes` (top) | free text; the first note titles the photo on the owner's page |
| `open_leaves` (top) | door openings whose leaves stand open in the photo; the render hides those leaves |

Because a pick names a feature rather than coordinates, a camera is always solved against the model as it stands:
move a wall and every pick on it moves too. A problem in the file is reported with the file, the pick's label and
the field or element (with a suggestion when an id is misspelled).

### Feature references

A reference names one element or opening by id, plus selectors that each pin coordinates of a point on it. The full
grammar is at the top of `overlay/features.py`.

| element | selectors |
| --- | --- |
| wall | `face` left, right, centre, +x, -x, +y, -y; `end` a, b; `level` base, top |
| opening | `face` (of its wall); `jamb` start, end; `level` sill, head; `casing` inner, outer (the casing's edges on that face, on its front plane); `light [i, j]` the edges of the unit's clear light in column i, row j (mullion and rail edges, on the unit's own faces) |
| box | `x`, `y`, `z`: min, max, centre |
| column | `part` shaft, capital; `x`, `y` min, max, centre; `level` base, top |
| beam | `side` (as a wall's face); `end` a, b; `level` soffit, top |
| stair | `riser k` (0 the top riser); `level` nosing, foot; `end` a, b |
| space | `vertex i` or `corner "+x+y"`; `x`, `y` min, max (an outline edge); `level` floor, ceiling |
| slab | as a space, with `level` top, bottom |
| roof_plane | `vertex i`; `edge [i, j]` (an edge along a building axis); `level` top, bottom (bottom is the thickness below: a fascia's lower edge) |

- "left" is the side to the left walking from `a` to `b`. A face word such as `+y` names the face whose outward
  normal points that way.
- An opening's `start` jamb is the one nearer its wall's `a` (at `at`), `end` the one at `at + width`. Which of them is
  on the left in a photo depends on which way the wall runs, so check before writing picks:
  `overlay.measure proj <stem> '{"el": ..., "jamb": "start", "level": "sill", "face": ...}'` says where it lands
  with any rough camera, and the wall's `a` and `b` are in house.json. Picks with start and end swapped make a camera
  that fails outright or puts many picks far off.
- An opening's trim is usually what the photo shows: `casing: inner` with a jamb and a level is the corner of the
  casing round the opening, `casing: outer` its outside corner, both on the casing's front face, which stands proud
  of the wall. Pick whichever edge shows against something darker or lighter.
- A list of references pins everything any of them pins: `[wall A face, wall B face]` is the inside corner of two
  walls. Two pins of one coordinate must agree to 0.02 in.
- `{"between": [ref, ref]}` is the midpoint of two references that pin the same coordinates (a mullion's centre).
- A coordinate nothing pins must be declared with `free`, which makes the pick a line pick. A forgotten selector then
  fails instead of quietly turning a point into a line.
- A sloped ceiling and a roof plane's top are heights: they give z where the rest of the reference pins x and y.
- A wall, opening or beam that runs along no building axis (a bay window's angled wall) pins vertical planes through
  its faces and ends; they meet in a point or a vertical line as usual, or, with a level, a horizontal line along the
  wall.

### Choosing picks

- **At least six picks, four of them full points** (picks that pin x, y and z, not line picks), and in practice a
  dozen or more. Spread them over the whole frame, to its corners, and over several depths: near and far, high and
  low.
- **Not all on one plane.** Picks on a single wall cannot separate the focal length and principal point from where
  the camera stands. Add a floor or ceiling line along a side wall, a counter corner, a corner of another wall. Line
  picks off the plane count: a camera whose full points all lie on one wall passes the guard when lines on another
  wall hold its depth, though the log still warns that the full points are coplanar.
- **Sharp corners beat soft ones.** An inside corner, a jamb against a bright window, a counter's front corner. Avoid
  features hidden by furniture or shadow.
- **Lines where heights are uncertain.** A vertical edge at a known plan position (`free: z`) is a good pick even
  when nobody knows the sill height. A floor line along a wall (`free` along the wall) holds the camera's height.
- **Checks for claims.** A feature whose position the model only assumes, or that seems to disagree, goes in as
  `role: check`: the solve does not see it, and its residual measures the disagreement instead of absorbing it.
- `sigma_px` says how far you trust a pick: 1 to 2 px for a crisp corner read at 8x, 3 to 6 px for a soft one.

## Solving

    uv run python -m overlay.solve living-01 [options]

What it does, in order: resolves and snaps the picks; detects the photo's line segments and tests whether its
verticals are parallel; solves the camera by robust least squares from several starts (the DLT, and PnP over a grid
of focal lengths); measures the held-out checks; applies the write guard; writes the camera, the render request and
`out/overlays/<stem>_overlay.png`. The log prints the residuals, the worst picks, the camera's parameters with their
uncertainties, the checks, and warnings about the picks' layout.

| option | default | when to change it |
| --- | --- | --- |
| `--attitude auto\|level\|zero_pitch\|free` | auto | auto takes `level` when the photo's verticals are parallel and the camera is not rolled (corrected, as in most listing photos), `zero_pitch` when they are parallel but rolled, `free` when they converge; force it when the test cannot tell |
| `--principal free\|dy\|fixed` | free | `dy` when only a vertical recomposition is plausible and dx is weakly held; the horizon starts dy for a level camera |
| `--aspect free` | fixed at 1 | when the photo was stretched vertically (common after perspective correction): residuals pointing up near the top of the frame and down near the bottom, or the reverse |
| `--distortion k1` (`k1k2`) | none | when straight edges near the frame's edges bow; each coefficient is solved under a prior (k1 0 +/- 0.15, k2 0 +/- 0.1) |
| `--use-lines` | off | adds the photo's long straight segments as constraints that point them at their building axis' vanishing point; they hold the attitude, focal length and principal point to a fraction of a pixel. Use it whenever the photo has clean architectural lines |
| `--focal F` | free | to hold a focal length known from another photo of the same camera |
| `--start CAMERA.json` | none | to start from another camera (a rough solve, or a neighbouring photo's); recorded, so a re-solve starts there again |
| `--hide-tags TAGS` | by where the eye stands | when the default hides too much or too little (below) |
| `--no-snap`, `--snap-radius PX` | snap on, 12 px | to ignore the picks' `snap` flags |
| `--dry-run`, `--force` | | solve and print only; write a camera the guard refuses |

Every option the solve ran with, as it resolved (the attitude auto chose, the starting principal offset), is recorded
in the camera JSON under `solve.options`, so a later re-solve repeats it exactly.

Reading the log: crisp picks on a good camera fit to about 1 to 2 px rms, with the sigma ratio (rms over the picks'
stated sigmas) near 1. A pick many sigmas off is named as an outlier and left out of the uncertainty: it is misread,
aimed at the wrong feature, or the model is off there. Checks noticeably worse than the solve's own picks mean the
camera or the model is off away from the picks. Each parameter prints with its uncertainty; the guard reads those.

### What the write guard refuses

A camera that the picks do not pin down would cost a render and then look like an answer, so the solve refuses to
write it, naming every reason, and says what in the picks' layout causes it.

| refusal | what to do |
| --- | --- |
| focal length uncertain by more than 20% | spread picks to the frame's edges and over more depths; or hold `--focal` |
| eye distance along the view uncertain by more than 36 in (or 3% of the picks' median depth, if larger) | add near and far picks; picks at one depth let the focal length trade with the distance |
| pixel aspect uncertain by more than 0.03 | add picks high and low in the frame, or keep `--aspect fixed` |
| no data constrains some parameters | the named parameters trade freely: hold one (`--principal dy`, `--attitude level`) or add picks off the plane they share |
| no redundancy | add picks: the solve has no spare rows to judge its fit by |
| the full points lie nearly on a line | add full points away from that line |

The usual causes it names: the full points lie on one plane (add picks off it), or the picks cover a small part of the
frame (spread them). A solve can also fail outright, naming why: a free parameter ran to its bound (the focal length,
the pixel aspect, a lens coefficient), a lens that folds over inside the frame, or full points behind the camera
(usually a mislabelled pick). The robust loss lists the picks it gave up on; look at those first.

### Pixel aspect and lens distortion

Listing photographers correct verticals in software, which shifts the principal point (usually up, dy negative) and
often stretches the image vertically. The stretch is the pixel aspect, fy / fx; a square-pixel solve absorbs it into
the focal length and distance and leaves a vertical residual pattern. Free it with `--aspect free`; it is bounded to
0.8 to 1.25 and refused at a bound.

Wide lenses bow straight lines near the frame's edges. Free k1 with `--distortion k1` (and k2 only when many picks sit
near the corners). The camera JSON records the lens; the render stays a pinhole image, and compose warps it into the
photo's frame so edges land where the lens put them. A solve's log reports each coefficient's uncertainty against its
prior; one barely better than its prior means the picks cannot see it.

## Renders and overlays

The render request (`overlay/requests/<stem>.json`) is what SketchUp draws: a centred, square-pixel pinhole image,
larger than the photo when the principal point is off-centre or the lens needs room. Compose cuts it back to the
photo's frame (rescaling for the pixel aspect, warping for the lens). Its `hide_tags` follow where the camera stands:
in a space of storey k, every zone of a higher storey is hidden (the roof, the floor above), so a room's render shows
the room; a camera outside hides nothing. `--hide-tags` overrides this. `open_leaves` become `hide_leaves`, drawing
those doors as open openings. A photo's render always shows today's house, never a design alternative.

`overlay.render` refuses unless house.json is the file the camera was solved on, and asks SketchUp with that sha256,
so SketchUp refuses too unless its HOUSE group was built from it. A render of another model through a photo's camera
would be scored as if it were this one.

**The working overlay** (`out/overlays/<stem>_overlay.png`): the photo dimmed, the model's edges in magenta; each pick
a yellow ring (magenta if the solve called it an outlier, a green diamond if a check), numbered in the camera JSON's
order; cyan the model feature projected (a cross, or a line for a line pick); red the residual; grey where a snap
moved the pick from. A header gives the solve's rms, the checks and the chamfer. Its numbers are in
`<stem>_overlay.json`.

**The clean overlay** (`<stem>_clean.png`, `overlay.compose <stem> --clean`): the model's edges on the dimmed photo
and nothing else, for the owner. It is refused when there is no usable render.

Reading an overlay:

- Everything off by the same shift or scale: the camera. Check the worst picks, the checks, and the options.
- One element off while its neighbours land: the model. Measure it (below) and fix the Starlark.
- Lines bowing apart toward the frame's edges: the lens (`--distortion k1`).
- Good near the picks, drifting away from them: too few or bunched picks; add some where it drifts.
- A model edge with no photo edge: something modelled that is not there, or hidden by furniture.

**The chamfer** scores a render with no picks at all: `render_to_photo_px` is the mean distance from each model edge
pixel to the nearest photo edge (truncated at 20 px), and `render_within_near` the fraction within 3 px. Lower is
better; compare a photo against itself across changes, since clutter and texture set each photo's floor. A render
older than its request (the camera was solved again since) is stale: reported, not drawn, not scored.

## After the model changes: re-stamping

    uv run python -m overlay.restamp [--trial] [--force] [STEM ...]

A camera is solved against the model as it stood. After the model changes, each photo is solved again from its
recorded options on house.json as it stands, rendered and composed; `look.py STEM ...` does this for the stems it is
given. With `--trial` nothing is written and SketchUp is not asked: each line says how the camera would move. A photo
whose camera, render and overlays already match house.json, with a render newer than `build/build.rb` and
`build/render.rb`, is skipped as up to date (`--force`, or `look.py --force`, re-does it). A camera the guard now
refuses is reported and the old one kept.

A large move after a small model change means the picks lean on the changed element; a camera that hardly moves while
its overlay improves is the model getting better.

## Measuring from photos

    uv run python -m overlay.measure bp living-01 812.5,431 950,600 --on y=6
    uv run python -m overlay.measure bp living-01 640,150 --on '{"el": "living.space", "level": "ceiling"}'
    uv run python -m overlay.measure bp living-01 1118,600 --on x=6,y=6
    uv run python -m overlay.measure bp living-01 950,500,1050,520 --on '{"el": "living.wall.front", "face": "left"}'
    uv run python -m overlay.measure proj living-01 \
        '{"el": "living.wall.front.door", "jamb": "end", "level": "head", "face": "left"}'
    uv run python -m overlay.measure cam living-01
    uv run python -m overlay.measure tri living-01 721.9,524.9 living-02 671.7,464.9     # with a second photo

- **bp** (back-project) reads where a pixel's ray meets a target: an axis plane (`y=6`), an axis line (`x=6,y=6`,
  the third coordinate free), a line through two points (`X,Y,Z/X,Y,Z`), or a feature reference naming a plane (a
  wall face), a height (a sloped ceiling) or a line (an inside corner). Several picks also print the step between
  them (a width, a rise); a line pick (`U0,V0,U1,V1`) reads its run and length. `--offset` moves the target.
- **tri** (triangulate) meets the rays of one feature seen in two or more photos, with each ray's miss and the widest
  angle between them (under about 10 degrees says little about depth).
- **proj** says where a model point or line lands in a photo; **cam** what a camera rests on.

A reading is only as good as its plane. A window head read on the wall's face plane is right only if the head lies in
that plane; a head set back in a deep reveal is not. Name the plane the feature truly lies on, and use `tri` when no
modelled plane holds it. A feature reference names it most surely: a casing's edge is on `{"el": ..., "face": ...,
"casing": "inner"}`, a window's glass or frame on `{"el": ..., "light": [0, 0], "face": ...}` (the unit's own plane,
inside the wall). Every reading prints how far it moves per pixel and that step times the camera's check rms: the honest
size of the error. A camera with no checks prints no such line, since nothing measures it away from its picks.

## The owner's review page

    uv run python -m overlay.gallery [--title TITLE] [--notes NOTES.md] [--out out/gallery.html]

One self-contained HTML file: a card per solved photo, grouped by the zone the camera stands in, with the photo under
its clean overlay and a slider that fades between them, and the solve's rms and checks. A card says so when its
camera was solved on an older house.json or its overlay predates its camera. `--notes` puts a short Markdown note at
the top: what changed and what to look at. Images are embedded, so the page can be sent or shared as it is; make it
again and send it after a batch of changes, for an owner away from SketchUp.

## The camera JSON

`overlay/cameras/<stem>.json` holds the camera (`image_size`, `eye`, `target`, `up`, `fov_v_deg`,
`principal_offset_px`, `pixel_aspect`, `distortion` when non-zero) and the record of its solve:

| key | contents |
| --- | --- |
| `residual_px`, `check_residual_px` | rms, max and count of the solve's residuals and of the held-out checks |
| `house` | the path and sha256 of the house.json the picks were resolved against |
| `eye_space` | the space the camera stands in (id and zone), or null outside |
| `correspondences` | every pick: label, model point, pixel, residual, whether the solve kept it, its reference and role |
| `solve.options` | every option, as it resolved: what a re-solve passes back |
| `solve.params`, `solve.std` | the solved parameters and their uncertainties |
| `solve.guard` | the write guard's limits, values, refusals and causes, and whether it was overridden with `--force` |
| `solve.warnings`, `solve.outliers`, `solve.geometry` | warnings, the picks the robust loss gave up on, the picks' layout |
| `verticals`, `horizon_dy_px` | the corrected-verticals test, and the principal dy the horizon gave |

## Files

| path | what |
| --- | --- |
| `sources/photos/<stem>.jpg` | the photos, untouched |
| `overlay/picks/<stem>.json` | the picks (written by hand) |
| `overlay/cameras/<stem>.json` | solved cameras (written by `overlay.solve`) |
| `overlay/requests/<stem>.json` | render requests (written by `overlay.solve`) |
| `out/renders/<stem>_edges.png` | SketchUp's renders (generated) |
| `out/overlays/<stem>_overlay.png`, `_clean.png` | the working and clean overlays (generated) |
| `out/annotate/<stem>/` | gridded crops (generated) |
| `out/gallery.html` | the owner's review page (generated) |

Picks, cameras and requests are committed; everything under `out/` is generated and can be made again.
