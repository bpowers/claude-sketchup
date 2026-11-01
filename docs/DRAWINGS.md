# Reading architect drawings into the model

Architect drawings are the most accurate source a house model has. A PDF exported from CAD carries the drafted lines
exactly, often on named CAD layers, and its dimension strings state the designer's numbers. This guide turns a set of
drawings into the Starlark under `zones/`, checking the model against the sheets as it grows. Photos
(`docs/PHOTOS.md`) cover what drawings miss, such as what was actually built, trim and fixtures, and check the result.

The example cottage's drawing set, `sources/plans/example-cottage.pdf`, is synthetic: two vector sheets with CAD
layers (page 0 the floor plan A1, page 1 the front elevation A2, both at 1/4" = 1'-0") drawn from the cottage itself
and registered as `plan_main` and `elev_front`. Every command below but step 7's (a scan) runs on it as written, so it
is the place to try a tool before using it on the owner's drawings.

## The tools

| command | what it gives |
| --- | --- |
| `uv run python -m extract.pdf info PDF` | pages, paper sizes, vector or scan, CAD layers |
| `uv run python -m extract.pdf render PDF --page N --dpi 100` | the page as a PNG to look at (cached in `out/pdf/`) |
| `uv run python -m extract.pdf layers PDF --page N` | how much each CAD layer draws on the page |
| `uv run python -m extract.pdf segments PDF --page N ...` | the lines, by layer and window, in points or model inches |
| `uv run python -m extract.pdf text PDF --page N ...` | text with positions, and the dimensions it states, in inches |
| `uv run python -m extract.pdf grid PDF --page N ...` | a zoomed crop with a labelled grid, for reading positions by eye |
| `uv run python -m extract.pdf calibrate PDF --page N ...` | the scale from a dimension, checked against others; writes a registration |
| `uv run python -m tools.plancheck NAME` | the compiled model's plan drawn over a registered plan sheet |
| `uv run python -m tools.plancheck NAME --elevation -y` | the model's facade drawn over a registered elevation sheet |

Each command's `--help` lists its options. `--page` counts from 0, as `info` lists the pages: a PDF viewer's page 3 is
`--page 2`. Positions on a sheet are PDF points (1/72 in of paper) in the page's own axes: origin at the bottom-left,
y up. A page saved with a rotation is displayed turned, but its coordinates stay in those axes, and `grid` labels them,
so a reading taken off a grid image always means the same point to every tool.

A PDF with no CAD layers (`info` says so) has every line on the layer `None`: name that where a command takes layers,
or leave the layers out.

## 1. Inventory the sheets

Run `info` on every PDF in `sources/plans/`, then render each page at about 100 dpi and look at it. Record one row per
sheet in `docs/HOUSE.md`: file and page, sheet number and title, what it shows (floor plan, reflected ceiling plan,
exterior or interior elevation, section, detail, schedule, site plan), its stated scale, vector or scan, the CAD layers
that matter, and whether it shows existing or proposed work.

One sheet often holds several drawings at different scales, such as a floor plan at 1/4 in = 1 ft beside an enlarged
kitchen at 1/2 in = 1 ft. Each drawing is registered on its own.

## 2. Existing or proposed

The model is the house as it stands today. Most drawing sets were made for a change: a permit set shows the proposed
house, demolition plans and existing-conditions sheets show what was there before, and as-built sets, which are rare,
show what was built. The usual conventions are existing walls hollow or lightly hatched, new walls solid or dark, and
demolished work dashed, often on layers such as `A-WALL-EXST`, `A-WALL-NEWW` and `A-WALL-DEMO`.

Whether a proposal was built, and how faithfully, is a question for the owner and the photos: add it to the questions
in `docs/HOUSE.md`. A proposal that was never built can become a design area (`docs/DESIGN.md`).

## 3. The scale

A stated scale is a claim. A PDF printed to fit its paper, or saved at another paper size, keeps the drawing's
proportions but loses its scale. So measure it from the longest overall dimension you can find, usually an exterior
wall's overall string. Take the two points it spans, in points, and calibrate with a check in the other direction:

```
uv run python -m extract.pdf calibrate sources/plans/example-cottage.pdf --page 0 \
    --a 300 220 --b 840 220 --real "30'-0\"" \
    --check 300 220 300 652 "24'-0\""
```

It prints inches of house per point, the nearest standard architectural scale and how far the measurement is from it,
and each check's residual. A vector sheet at a standard scale measures within about 0.1% of its nominal scale, and its
checks agree within 1/4 in. The check across the other direction is what catches a stretched scan or a print that was
squeezed to fit.

Where the points come from: the dimension's extension lines, from `segments` on the dimension layer, or the wall faces
the string runs to. On a sheet with no layers, or one whose dimensions are not lines, the exterior walls' outer faces
are among the longest lines on it, and `--max-length` a little over the house's longest wall leaves out the page border
and the title block's frame (on the example, `segments sources/plans/example-cottage.pdf --page 0 --min-length 100
--max-length 600`). `text` lists the strings with their positions (step 5).

## 4. Register the sheet

A registration places a sheet in the model's frame: the sheet point that lands on the model's origin, the scale, and
the turn from the sheet's axes to the model's. Choose the model's origin and axes once, in `docs/HOUSE.md` (usually
the outer corner of two main exterior walls, with +x along the main plan's horizontal), and register every plan sheet
so that the same corner of the house lands on the same model point. Name point `a` the corner and add its model
coordinates:

```
uv run python -m extract.pdf calibrate sources/plans/example-cottage.pdf --page 0 \
    --a 300 220 --b 840 220 --real "30'-0\"" --a-model 0 0 --register plan_main
```

This writes `plan_main` into `sources/plans/registrations.json` (making the file the first time), which every Python
tool reads. `--rotation DEG` turns a sheet drawn at an angle to the model's axes.

Then confirm it: `segments --registration plan_main --layer A-WALL --axis h --min-length 24 --merge 1` should report the
exterior faces where you expect them (0.0 and the overall), and plancheck (step 9) should show the walls on the sheet's
lines.

Starlark cannot read the file. Where the Starlark cites a point read straight off the sheet, one no dimension states
(a post's centre, a jog in a wall), it names the sheet point through a copy of the registration in `lib/frame.star`,
so that correcting the registration moves every such number:

```python
PLAN_MAIN = registration([300.0, 220.0], 0.66666667)              # as calibrate --register prints it
PORCH_POST_AT = sheet_to_model(PLAN_MAIN, [561.0, 121.0])        # A1: the post's centre, which no dimension locates
```

`tests/test_pdf.py` fails when a copy in `lib/frame.star` and the file disagree. Numbers that come from dimension
strings go in through `chain()` instead (step 6), and need no copy.

## 5. Walls, openings and casework from the lines

`layers` lists a page's CAD layers and how much each draws. Names follow the US National CAD Standard (`A-WALL`,
`A-DOOR`, `A-GLAZ`, `A-FLOR-CASE`, `A-ANNO-DIMS`, `I-FURN`) or the firm's own (`WALL - EXTERIOR`, `CABINETS`,
`DIMENSIONS`). Read the lines in model inches:

```
uv run python -m extract.pdf segments sources/plans/example-cottage.pdf --page 0 --registration plan_main \
    --layer A-WALL --axis v --min-length 12 --merge 1 --window -12 -12 372 300
```

- A wall is two parallel face lines. Its thickness is their distance apart and its centreline their midpoint, which
  is what a `wall` element takes (`docs/IR.md`). Check the wall-type legend for what the lines include: exterior walls
  are often drawn to the sheathing or the stud face, and the finish may need adding.
- An opening is a gap in both face lines, closed by short jamb lines across the wall. Its `at` is measured from the
  wall's `a` end: `along(a, b, [x, y])` in `lib/geometry.star` turns a jamb's point into it. A window usually draws its
  glass inside the gap. Heads and sills come from elevations (step 8) or a door and window schedule.
- A door on the door layer draws its leaf, a line square to the wall from the hinge, and an arc from the leaf's tip to
  the far jamb. The hinge is the leaf's foot and the swing goes to the side the leaf stands on: in the example, the
  bedroom door's leaf stands at the partition's jamb at y 90 on its +x side, the partition runs `a -> b` along +y, so
  `swing("start", "right")`. `hinge_jamb(a, b, at, width, point)` and `swing_side(a, b, point)` read the same from the
  leaf's foot and a point in the room it opens into. plancheck draws each swing back over the sheet, so a wrong one
  shows.
- Casework draws its outline: the counter's front edge solid, uppers overhead dashed. Appliances often have a layer of
  their own.
- `--merge GAP` joins a dashed line, or a wall face broken at every junction, into the one line it is, before
  `--min-length` drops hatching and lettering strokes. A piece drawn twice is listed once. A line can still come out
  twice when a stroke was exported as a thin filled outline; `fill` marks those.

`text --registration plan_main --match "['\"]"` lists every string with a foot or inch mark, with its position, model
position and value in inches; the position says which wall a string dimensions. Characters are joined into strings by
where they stand, so a string a plotter wrote letter by letter reads whole; one that reads oddly (two numbers run
together, a label cut in two) is checked with `grid`. When a dimension and a measured line disagree by a little,
prefer the dimension, since it is the designer's number and the linework is drafted to it, and note which you used.
Some sheets draw their text as outlines, which is not text: `text` finds nothing there, and the numbers are read with
`grid`.

## 6. Chains that close

Write dimensions into Starlark as the drawing writes them, a run with its overall, through `chain()` in
`lib/units.star`:

```python
PLAN_X = chain([["living room and kitchen", "20'-0\""], ["bedroom and bath", "10'-0\""]],
               closes = "30'-0\"", name = "A1, the front wall's string")
```

A run that does not sum to its overall fails the compile and names the chain, so a misread number is an error instead
of a crooked wall. A span the drawing marks EQUAL, or leaves undimensioned, is `"FILL"`, solved from the overall. Keep
each number a named constant with a comment citing its sheet and what it measures (`# A2.1, the front wall's string,
to the stud faces`), and set `src` on the elements it shapes: `cad` for a vector sheet, `scan` for a scan.

## 7. Scans

Older sets are scans: each page is one image (`info` says `scan`), so there are no lines or text to extract, and the
paper may be stretched or skewed. Read a scan at 300 to 400 dpi with `grid`. Its labels are points, so a scan is
calibrated and registered exactly as a vector sheet is:

```
uv run python -m extract.pdf grid sources/plans/1962.pdf --page 0 --center 410 233 --size 40 --dpi 400
uv run python -m extract.pdf grid sources/plans/1962.pdf --page 0 --center 410 233 --size 40 --dpi 400 \
    --mark 409.6 232.8
```

Look at the image, read the two extension lines of a long dimension, and calibrate as in step 3, with a check across
the other direction. Put a `--mark` on a reading to confirm it: the crosshair should sit on the line. If the two
directions disagree by more than about 0.5%, the scan is stretched: register each part of the sheet you use on its
own, keep each reading local, and lean on the written dimensions more than on measured lengths. Hand lettering is read
by eye off the grid image, and the drafted lines check it.

## 8. Heights: elevations and sections

Plans give x and y. Heights come from exterior and interior elevations, building sections and schedules:
floor-to-floor heights, plate and ceiling heights, window heads and sills, counter and upper-cabinet heights, roof
pitch and overhangs. Every z in the model is absolute (`docs/IR.md`), so a note such as `7'-0" A.F.F.` (above finished
floor) is added to the height of the floor it stands on.

An elevation registers like a plan, its axes read as the distance along the facade and the height: point `a` is a
corner of the facade on the main floor, and its model point is `--a-model U 0`, where U is that corner's coordinate
along the facade as the sheet runs left to right, seen from outside. That is x for a facade facing -y (the sheet's
left is the model's low x), -x for one facing +y, y for one facing +x and -y for one facing -x; so the example's front
elevation, which faces -y, registers its west corner at U 0:

```
uv run python -m extract.pdf calibrate sources/plans/example-cottage.pdf --page 1 \
    --a 300 250 --b 840 250 --real "30'-0\"" --a-model 0 0 --register elev_front
```

Sections and interior elevations register the same way: U runs as the sheet runs left to right for the viewer the
drawing assumes (an interior elevation looking toward +y runs along x), and point `a`'s model point is `--a-model U Z`,
with Z its floor's height when that is not the main floor.

Then `segments --registration elev_front --axis h` reports each level line's height as its y: floor lines, sills,
heads, plate and ridge. A roof's pitch is on a section or elevation as a pitch symbol (such as 6:12), or measured as
rise over run between two points on the roof line.

plancheck checks heights the way it checks a plan (step 9): `--elevation FACING` draws the facade over the registered
elevation sheet, its exterior walls, openings, roof edges, outdoor slabs, steps, posts and beams, and every datum as a
dashed line, and measures each level model line (sills, heads, wall tops, eaves) against the sheet's level lines.
Photos (`docs/PHOTOS.md`) check heights too, and see what the elevations do not.

## 9. Check with plancheck after each batch

After each batch of walls, openings or casework, compile and draw the model over its sheet:

```
uv run python look.py --compile-only
uv run python -m tools.plancheck plan_main --layers A-WALL --ids
```

This writes `out/plancheck/plan_main.png`: the sheet faded to grey under the model's plan cut 48 in above z = 0. Walls
are red, windows cyan, doors green with their swings, cased openings orange, spaces dashed blue, boxes on the floor
orange and anything overhead (upper cabinets, beams) dashed purple. Look at the whole image: where red and the sheet's
wall lines part, one of them is wrong. plancheck also measures each wall face against the nearest parallel line on the
layers `--layers` names and prints the offsets (positive when the sheet's line stands outside the model's face); any
over `--tol` (1 in) is written beside the face on the image. A sheet with no CAD layers needs no `--layers`: its faces
are measured against every line on it, so a dimension or border line can match too, and the image stays the judge.

The elevation is the same check for heights:

```
uv run python -m tools.plancheck elev_front --elevation -y --layers A-ELEV --ids
```

writes `out/plancheck/elev_front.png` and prints each sill's, head's, wall top's and eave's offset from the sheet's
nearest level line (positive when the sheet's line is higher). `--depth` (36 in) is how far behind the facade's front
an exterior wall still draws.

`--z` cuts a plan at another absolute height (an upper floor: its floor's z plus about 48), `--window X0 Y0 X1 Y1`
zooms to a part of the house in model inches (u and z for an elevation), with `--dpi` raised to match, and `--plain`
draws the plan or the elevation alone on a one-foot grid, to scale, with no sheet.
