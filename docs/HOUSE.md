# The house brief

The one page about this house: what the model is for, how its frame is set, what each source settles, what each area
holds, and what is still open. Read it at the start of every session and keep it current as facts settle. It states
facts, not a history: when something changes, change the line.

A new house fills in this template with the owner (CLAUDE.md, "Starting a new house"). Until then it holds the example
cottage's values, each marked *(example)*: replace them with this house's, and delete the example rows. The quoted
paragraph under each heading is that section's standing rule, how to fill it and keep it current; it stays.

## Purpose and scope

> The owner's goal in their own words, and the areas in the order they matter. Give each area a fidelity:
> **detailed** (openings with their units and casings, casework with its doors and drawers, appliances, fixtures,
> ceilings, beams, columns, and the trim the photos show), **broad** (walls, openings, floors and ceilings in the right
> places; casework as blocks) or **massing** (the shell only). Detail nobody needs costs time and adds errors to check.

Goal: *(example)* "We want to open up the kitchen, and see an island or a peninsula before we talk to a contractor."

People: *(example)* two adults, 5'-4" and 6'-0", one cook at a time. (Design work derives counter heights, reaches and
clearances from them, not from today's fixtures.)

| Area | Zones | Fidelity | Why |
|---|---|---|---|
| Kitchen *(example)* | kitchen | detailed | the remodel |
| Living room *(example)* | living | broad | seen from the kitchen, and where the stools face |
| The rest *(example)* | bedroom, bath, porch, roof | massing | context |

## Compass and axes

> The owner speaks in their own directions. Translate every one through this table before moving anything. Settle it
> with the owner and the plan's north arrow, and add the words they actually use ("the street side", "toward the
> garden").

| The owner says | Model axis |
|---|---|
| north, the back *(example)* | +y |
| south, the front, the street side *(example)* | -y |
| east, right as seen from the street *(example)* | +x |
| west, left as seen from the street *(example)* | -x |
| up, down | +z, -z |

True north: *(example)* +y is north, so `LOCATION` in `house.star` has `north_angle` 0 (the degrees SketchUp turns north
clockwise from +y; it sets the sun for shadows).

## Frame and origin

> Choose the frame once, before the first wall, and register every plan sheet to it (docs/DRAWINGS.md, steps 3 and 4).
> The usual choice: the origin at the outer corner of two main exterior walls, +x along the main plan sheet's
> horizontal, +y up the sheet, z = 0 the main floor's finish floor. `lib/frame.star` holds the same in code.

- Origin: *(example)* the outer corner of the west and front walls.
- Axes: *(example)* +x along the front wall's outer face, west to east; +y from the front wall toward the back; +z up.
- z = 0: *(example)* the finish floor of the main storey.
- Main sheet: *(example)* `plan_main`: `sources/plans/example-cottage.pdf` page 0, sheet A1, the outer corner at
  (300, 220) pt, mirrored as `PLAN_MAIN` in `lib/frame.star` for the point Starlark reads off it.

## Levels

> Every height the house is built on, absolute (z = 0 above), with where it comes from. A drawing's note such as
> 7'-0" A.F.F. is added to the z of its floor. Each level is a named constant, in `lib/frame.star` or in the zone that
> owns it, and a datum in `house.star`.

| Level | z (in) | Constant | Source |
|---|---|---|---|
| main finish floor *(example)* | 0.0 | `MAIN_FLOOR` | the frame's choice |
| ceilings *(example)* | 96.0 | `CEILING` | A1, "CLG 8'-0"" in each room |
| exterior walls' top plate *(example)* | 104.0 | `WALL_TOP` | A2, "T.O. PLATE 8'-8"" |
| porch slab *(example)* | -14.0 | `PORCH_TOP` | two 7 in risers below the main floor (A2 draws the steps) |
| grade at the walls *(example)* | -18.0 | `GRADE` | A2's grade line |
| ridge *(example)* | 184.0 | `ROOF_RIDGE_Z` | A2, "RIDGE 15'-4""; a 6 in 12 pitch over the top plate |

## Sources

### Drawings

> One row per drawing, from `extract.pdf info` and a look at every page (docs/DRAWINGS.md, step 1); a sheet that holds
> several drawings at different scales gets a row for each. "Settles" says what this drawing is the best evidence for.

| File, page (from 0) | Sheet | Shows | Scale | Vector or scan | Layers that matter | Existing or proposed | Registration | Settles |
|---|---|---|---|---|---|---|---|---|
| *(example)* `example-cottage.pdf`, 0 | A1 | floor plan | 1/4" = 1'-0", measured +0.00% | vector | A-WALL, A-DOOR, A-GLAZ, A-FLOR-CASE, A-ANNO-DIMS | existing (synthetic, drawn from the model) | `plan_main` | walls, openings and casework in plan |
| *(example)* `example-cottage.pdf`, 1 | A2 | front elevation | 1/4" = 1'-0" | vector | A-ELEV | existing (synthetic) | `elev_front` | floor, sill, head and plate heights; the roof's eave and ridge |

### Photo index

> One row per photo in `sources/photos/` (docs/PHOTOS.md). Status is `listed` (looked at, no picks yet), `picked`,
> `solved` (camera written) or `matched` (its overlay looked at whole, every mismatch fixed or listed under Open
> questions).

| Stem | Where from, looking toward | Shows | Status |
|---|---|---|---|
| *(example)* `living-01` | the living room's back corner by the kitchen opening, toward the front door | the front wall with its door and window, the west wall and its window (synthetic) | solved |

### The owner's word

> Facts the owner settled that no drawing or photo shows: what an object is, what changed since the drawings, what is
> behind a wall. One line each. The elements they shape carry `src = "owner"`, and their constants cite the answer.

- *(example)* The bath's high window is obscure glass, which no drawing says.

## Area inventories

> For each area in Purpose and scope, the complete list of its features: every wall, opening, built-in, appliance,
> fixture, ceiling feature and piece of trim that the drawings or photos show, modelled or not, with the photos that
> see it. Match what a photo shows against this list before deciding that something is missing or new. Status is
> `modelled`, `placeholder` (a box of kind `placeholder` until it is identified), `not modelled` (say why) or
> `question` (listed under Open questions).

### Kitchen *(example)*

| Feature | Element id | Status | Source | Photos |
|---|---|---|---|---|
| back wall and the window over the sink | `kitchen.wall.back`, `kitchen.wall.back.window` | modelled | A1 | none yet |
| base cabinets and counter along the back wall | `kitchen.base.back`, `kitchen.counter.back` | modelled | A1 | none yet |
| uppers either side of the window | `kitchen.upper.left`, `kitchen.upper.right` | modelled | A1 (dashed) | none yet |
| refrigerator | `kitchen.fridge` | modelled | A1 | none yet |
| cased opening to the living room | `living.wall.kitchen.cased` | modelled | A1 | none yet |
| pendant over the sink | | not modelled | | lights are out of scope |

## Open questions

> What is not known yet, each with who or what can settle it (the owner, a photo, a tape) and what it holds up. Ask the
> owner the cheap ones (what something is, whether it exists, what changed) as they come up; read positions and sizes
> from photos before asking for a tape. When a question is settled, delete it and put the answer where it belongs: a
> constant's comment, a row above.

- *(example)* Is the kitchen ceiling flat at 8'-0", or does it follow the roof? The owner. Holds up the kitchen's
  ceiling and the uppers' tops.
