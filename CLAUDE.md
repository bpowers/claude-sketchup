# CLAUDE.md

This repository is one house, modelled as code and drawn in SketchUp Pro. The house is written in Starlark
(`house.star`, `zones/`, `lib/`), compiled to one flat JSON document (`house.json`), checked by a planner
(`ir/plan.py`) and drawn by Ruby inside the owner's SketchUp through a file bridge (`bridge/`). The evidence is the
architect's drawings in `sources/plans/` (exact CAD lines and dimensions) and photos in `sources/photos/` (solved
cameras whose renders of the model are laid over them). Remodel ideas are design areas that the owner switches on and
off with SketchUp tags.

The loop, every time:

1. Edit the Starlark.
2. Run `uv run python look.py`: it compiles, plans, composes the design areas and syncs SketchUp in place, in seconds.
3. Tell the owner what to look at in SketchUp.

Until the owner's house replaces it, the house here is the example cottage: a small single-storey house that uses every
element type and has one design area (`kitchen`). Its drawing set (`sources/plans/example-cottage.pdf`) and its photo
(`sources/photos/living-01.jpg`) are synthetic, drawn from the cottage itself, so every command in the docs can be
tried on it.

## Read first

- `docs/HOUSE.md`: this house. The goal and scope, the owner's compass mapped to model axes, the frame, levels,
  sources, what each area holds, open questions. Read it at the start of every session and keep it current.
- `docs/IR.md`: the house.json contract, every element type and field. Where code and it disagree, it decides.
- `docs/DRAWINGS.md`, `docs/PHOTOS.md`, `docs/DESIGN.md`: reading drawings, matching photos, trying remodels (DESIGN.md
  also defines the areas, alternatives and tag chains IR.md's manifest uses).
- `bridge/README.md`: the bridge, its protocol and troubleshooting. `furnishings/README.md`: loose furniture.

Run every command from the repository root with `uv run`. Each command has `--help`.

## The repository

| Path | What it is |
|---|---|
| `house.star` | the entry: zones assembled into the document, with `PROJECT`, `ZONES` (storeys), `DATUMS`, `LOCATION`, and `CHECKS` across zones |
| `lib/frame.star` | this house's frame: axes, the plan's dimension strings as chains, wall faces, levels, shared details, sheet registrations |
| `lib/geometry.star` | one constructor per element type (`space`, `wall`, `opening`, `box`...), each checking its input and failing with the element's id; helpers such as `along`, `swing_side`, `hinge_jamb`, `ortho_union` |
| `lib/units.star` | `inches("12'-6 1/2\"")`, `chain()` (a dimension run that must close on its overall), `equal_bays()`, `solve_glass()` |
| `lib/checks.star` | checks across elements: `stair_joins`, `column_bears`, `sills_on_floors`, `all_at` |
| `zones/<zone>.star` | the house, one file per zone; every number a named constant with its source |
| `zones/areas.star`, `zones/areas/` | design areas: a design file per area and one file per alternative |
| `house.json` | the compiled house; committed, never edited by hand (regenerate it on a merge conflict) |
| `housec.py`, `areasc.py` | the compilers: Starlark to house.json; design areas to view documents, plans and the manifest |
| `ir/plan.py`, `ir/geom.py` | the planner (the only gate: a document it plans always builds) and shared geometry |
| `look.py` | the loop |
| `build/build.rb`, `build/render.rb` | drawing and rendering inside SketchUp; reloaded on every command that uses them |
| `build/palette.json`, `build/fixtures/` | the finishes; hand-written test documents, one per element family, and `refused.json` |
| `bridge/` | `watcher.rb` (inside SketchUp), `client.py`, `smoke.py` (the live check), `furnishings.py` and `.rb` |
| `furnishings/` | `pieces.json` (loose furniture) and `components/` (.skp product models) |
| `overlay/` | the photo tools; `picks/`, `cameras/`, `requests/` hold each photo's committed files |
| `extract/`, `tools/plancheck.py` | reading PDFs; the model's plan or elevation drawn over a registered sheet |
| `sources/` | the owner's drawings and photos, which are never altered, and `plans/registrations.json` |
| `tests/`, `out/` | pytest files with `tests/ruby/` (a stand-in SketchUp for plain Ruby) and `tests/fixtures/`; `out/` is generated and gitignored |
| `.claude/settings.json` | the commands Claude may run without asking: the loop, the drawing and photo tools, the tests |

## Writing the Starlark

- A zone file loads what it needs (`load("lib/frame.star", "CEILING", ...)`) and ends with a list of its elements,
  which `house.star` loads and joins. Make every element with a constructor from `lib/geometry.star` (its docstring
  gives the arguments), never as a raw dict: the constructors check what the contract states.
- A drawing's dimension string goes in through `chain()`, so a misread number fails the compile instead of making a
  crooked wall. Every element constructor takes `src` (it has no default), and every number that matters is a named
  constant citing its source (The working method, Evidence).
- Loaded modules are inlined into one namespace, and a top-level name defined in two modules fails the compile, naming
  both. So every top-level name a zone file defines starts with its zone (`KITCHEN_WINDOW`, `PORCH_TOP`,
  `kitchen_base`); `lib/frame.star`'s shared names need no prefix.
- This Starlark has no `**` power operator, no `pow`, `sum`, `round`, f-strings, `while`, `set` or math module; `for`
  and `if` statements work only inside a `def` (a comprehension or `x if c else y` works at the top level); and `%`
  takes no precision (`"%.2f"` fails; `%s`, `%d` and `%f` work). Round numbers for messages with `f2()` from
  `lib/geometry.star`; use `sqrt()`, `total()` and `fmt()` from `lib/units.star`.
- The constructors are stricter than the planner; for one, an outline runs counter-clockwise with no repeated closing
  point (`rect()` and `ortho_union()` give one).
- A wall's top runs straight from `top_a` to `top_b`, so a gable end is two walls meeting under the ridge, each from its
  eave's height to the ridge's, both taken from the roof's own function (the cottage's `roof_underside_z` in
  `zones/roof.star`, with the convention it states). An opening that runs to the wall's end, such as at that split,
  has no room for casing there: give `casing(width = {...})` 0 on that side.
- An opening's `at` runs from its wall's `a`: `along(a, b, point)` turns a point the drawing gives (a jamb's station on
  a dimension string) into it, and `hinge_jamb()` and `swing_side()` turn a drawn door leaf into its `swing`
  (docs/DRAWINGS.md, step 5).
- A check that spans zones (a stair landing on its floor, a post under its beam) goes in `CHECKS` in `house.star`,
  built from `lib/checks.star`.

## Starting a new house

The owner has put their drawings in `sources/plans/` and photos in `sources/photos/`. Work through these steps in
order, showing the owner the model at each stage.

1. **Inventory the sources.** Run `uv run python -m extract.pdf info sources/plans/<file>.pdf` on every PDF, render
   each page (`uv run python -m extract.pdf render sources/plans/<file>.pdf --page N --dpi 100`; pages count from 0)
   and look at it. Look at every photo whole. Fill the Drawings table and the photo index of `docs/HOUSE.md`. Give each
   photo a short lowercase stem (`living-01.jpg`) by renaming it before any picks name it. A HEIC or WebP photo is
   converted once, at full size, to JPEG (docs/PHOTOS.md, step 1); otherwise never crop, resize, rotate or re-save
   one.
2. **Interview the owner**, in one short message:
   - the goal, and which rooms matter first;
   - which side faces the street and which way is north, and the words they use for directions;
   - the levels: storeys, steps between rooms, a basement or crawl space;
   - what changed since the drawings, and which drawing set was actually built;
   - whether a renderer extension such as Enscape is open on the model (it makes rebuilds slow; Speed, below).
3. **Fill in `docs/HOUSE.md`**: purpose and scope with a fidelity per area, the compass table, levels as far as known,
   a first inventory of the priority areas, open questions. Replace the cottage's example rows.
4. **Choose the frame and register the main sheet**, the plan that shows today's house (docs/DRAWINGS.md, steps 3 and
   4). First delete `sources/plans/registrations.json`, which holds only the cottage's sheets. Calibrate the scale on a
   long dimension with a check across, and register the sheet with `--a-model 0 0 --register plan_main`. Record the
   frame in `docs/HOUSE.md`. With no plan at all, ask the owner to tape the outside walls' overall lengths and a few
   rooms, build the shell from those (`src = "measured"`), and let the photos refine it.
5. **Replace the example cottage.** Read its files once first; they are the worked example of every element type.
   - Remove its design areas and its synthetic sources (the design loads `zones/kitchen.star`, and a house with no
     `zones/areas.star` has no design areas):

         rm -r zones/areas.star zones/areas sources/plans/example-cottage.pdf sources/photos/living-01.jpg \
             overlay/picks/living-01.json overlay/cameras/living-01.json overlay/requests/living-01.json \
             tests/fixtures/example_sources.py tests/test_example.py

   - `lib/frame.star`: keep the registration helpers at the top (`registration`, `sheet_to_model`); replace the rest
     with this house's frame.
   - `zones/`: replace the cottage's files with one per zone of this house, each ending in a list of its elements.
     Start with the exterior walls (step 6): an empty house fails a few tests.
   - `house.star`: `PROJECT`, `ZONES`, `DATUMS`, `LOCATION`, `ELEMENTS` and `CHECKS`. A project name other than
     "Example cottage" retires the cottage's own tests, which then skip.
   - `furnishings/pieces.json`: take the cottage's furniture out of SketchUp first
     (`uv run python bridge/furnishings.py remove living_sofa living_rug bedroom_bed`), then empty the list
     (`"pieces": []`) until the owner says what furniture to place.
   - Run `uv run python look.py --compile-only` and `uv run pytest -q`, then `uv run python look.py`: the sync erases
     the cottage and draws the house. The cottage's tags stay in SketchUp until the owner purges them (README.md,
     Troubleshooting, says how).
6. **The shell first**: the exterior walls from the plan's vectors, a space per room with its floor and ceiling, the
   roof planes, then the interior walls. Every dimension string goes in as a `chain()` that must close. Ids follow the
   cottage's pattern, `<zone>.<what>[.<which>]` (`kitchen.wall.back`, `kitchen.base.back`, `kitchen.fridge`,
   `porch.slab`), an opening its wall's id and a name (`kitchen.wall.back.window`); keep them stable, since picks,
   design layers and build errors name them.
7. **Openings**: positions from the plan's wall gaps and door arcs, heights from elevations and schedules; then units
   (frames, mullions, rails) and casings where the area is detailed.
8. **Interiors in the priority rooms**: casework with its seams and rails, appliances, fixtures, ceilings (sloped
   planes, soffits), beams, columns, stairs. Everything else stays broad or massing, as `docs/HOUSE.md` says.
9. **Check against the drawings after each batch**: `uv run python look.py --compile-only`, then
   `uv run python -m tools.plancheck plan_main --layers <wall layers> --ids` (a sheet with no CAD layers needs no
   `--layers`) and look at `out/plancheck/plan_main.png` whole. Heights go the same way over a registered elevation:
   `uv run python -m tools.plancheck elev_front --elevation -y --layers <elevation layers> --ids` (docs/DRAWINGS.md,
   steps 8 and 9).
10. **Photos** (docs/PHOTOS.md): picks, `uv run python -m overlay.solve <stem>`, `uv run python look.py <stem>`, then
    look at the overlays. Start in the priority rooms, with photos from more than one direction in each.
11. **Design areas** (docs/DESIGN.md), once today's priority rooms match their photos.

## The everyday loop

    uv run python look.py                     # compile, plan, design areas, sync SketchUp: seconds
    uv run python look.py living-01           # the same, then re-solve, render and compose that photo (or several)
    uv run python look.py --compile-only      # compile, plan, design areas; no SketchUp
    uv run python -m tools.plancheck plan_main --layers A-WALL --ids   # the plan over its sheet
    uv run python -m tools.plancheck elev_front --elevation -y --layers A-ELEV   # an elevation over its sheet
    uv run python -m tools.plancheck --plain --ids                     # the plan alone, no sheet, no SketchUp
    uv run python bridge/furnishings.py place                          # loose furniture after an edit to its list
    uv run python bridge/client.py views [--view NAME]                 # overview pictures in out/views/<NAME>/

A look prints each step, then the sync (`built 2, instanced 0, kept 34, erased 2`: only what changed is drawn), then
`owner state: unchanged`, then one line of timings with the house's sha. It exits 1 when a step failed or the sync
changed the owner's state; say so to the owner. A compile error names the Starlark file and line; a refusal names the
element. A photo whose overlays already show this house.json, rendered by this build.rb and render.rb, is skipped as
up to date (`--force` re-does it). While SketchUp is closed or the owner is away, work with `look.py --compile-only`
and `plancheck --plain`.

You see images by reading PNG and JPEG files. A large image is scaled down when read, so zoom before judging detail:
`uv run python -m overlay.annotate <image> --at U,V` crops any image (a photo, an overlay, a plancheck) at 8x with a
pixel grid; `extract.pdf grid` does the same for a sheet; plancheck takes `--window X0 Y0 X1 Y1`.

## The working method

### What the model is

- The model is generated from source, never drawn. SketchUp is the build target and the owner's window. Every change
  goes into the Starlark: anything edited by hand inside `HOUSE` is lost whenever a sync redraws it. What the owner
  does by hand lives outside it (furniture in `FURNISHINGS`, their own groups, their tag choices), and no build
  touches that.
- Model the house as it stands today. Read older drawings for structure, hidden parts and original dimensions, never to
  model an earlier state. A remodel idea is a design area over today's house, never an edit to it, because the photos
  show today's house and every idea needs the same base.
- Scope by the owner's goal: the rooms they want to change in detail, their surroundings broadly right, the rest
  massing (`docs/HOUSE.md`, Purpose and scope).
- The model is right when the clean overlays of the photos look right and correctly proportioned to the owner, and the
  plan and elevation checks sit on the drawings.

### Evidence

- Each source settles different things:
  - The owner settles identity, presence, topology and today's use: what an object is, whether something exists, what
    connects to what, what changed since the drawings. These questions are cheap; ask them.
  - Vector drawings give exact plan positions and dimensioned heights. Plan positions from CAD are strong; heights are
    usually the weak axis.
  - Photos, through solved cameras, give the positions and sizes the drawings lack, and say what was actually built.
  - A tape or laser is for what no photo can see or tell apart. Measure from anchors and cameras first, to an inch or
    two, rather than turning every uncertain number into a request for the owner's tape.
- Every element carries `src` (`cad`, `scan`, `photo`, `owner`, `measured`, `assumed`). Every number that matters is
  a named constant with a comment saying where it comes from: the sheet and the dimension string, the photo and how it
  was read, the owner's words. That comment is the record, so keep it true when the number changes.
- Say plainly what is not known, and keep it under Open questions in `docs/HOUSE.md`.

### Drawings (the steps are in docs/DRAWINGS.md)

- Architect PDFs plotted from CAD often keep the CAD layers. Extract the vectors and use exact coordinates; reading a
  rendered sheet by eye is the last resort when vectors exist.
- Measure the scale on a long known dimension and check it against a second across it. A disagreement of more than a
  fraction of a percent with a stated dimension means a wrong reading.
- Register each sheet once (origin, scale, rotation, in `sources/plans/registrations.json`); every coordinate taken
  from that sheet goes through it, and a point the Starlark cites straight off the sheet goes through its copy in
  `lib/frame.star`.
- Dimension chains must close. A span the drawing never states (an EQUAL run, a remainder) is solved from the chain's
  closure (`chain()` with `"FILL"`), never guessed.
- Drawings show intent; the built house can differ. A permit set's existing and demolition layers show the house
  before its work, and its new work the house after: which is today depends on whether the work was built, and the
  owner and the photos decide. Work that was never built can become a design area.
- Scans: rasterise, calibrate from a dimension, read coordinates with `grid` at zoom, and register them against a
  vector sheet or the model where they overlap.

### Photos (the steps are in docs/PHOTOS.md)

- Solve each camera from picks on modelled features: at least 6, at least 4 of them full points, spread over the frame
  and over depth. Bunched or nearly coplanar picks give a camera whose residuals look fine and whose overlay is wrong
  elsewhere.
- Prefer anchors: features exact from the drawings (wall corners, door jambs, dimensioned heights, stair nosings). A
  camera that disagrees with an anchor is suspect; re-solve it with the anchors before trusting any reading from it.
- Listing and phone photos are often stretched by vertical correction and bowed by the lens: let the solve free the
  pixel aspect and k1 when the picks support it. A solved aspect far from 1 is a warning.
- Hold out a feature you are testing as a `role: "check"` pick, so a disagreement is measured, not absorbed.
- Back-project onto the plane the feature actually lies on: a cabinet's front face, not the wall behind it; a column's
  near face, not its axis. Reading on the wrong plane is the most common measuring error.
- One depth reading from a wide lens in a small room is weak; cross-check it in a photo from another direction before
  moving anything. Several photos can share a bias: before photos move an element against a tight drawing dimension,
  check that a nearby anchored element reads true on the same cameras.

### Looking

- Look at every overlay yourself, whole, each time you judge the model: crop and zoom 3 to 4x region by region
  (`overlay.annotate` on the overlay's PNG writes 3x tiles), list every mismatch with a pixel box, and fix or record
  each one. Never explain a visible mismatch away; if you blame the camera or the lens, name the evidence. A review
  of only what changed lets a standing error survive.
- Zoom in 4 to 5x on anything new and small, from the camera the owner will see it from and one other.
- Before calling something a defect, make sure the picture is current: built from the sources you are judging.
- Match what a photo shows against the area's inventory in `docs/HOUSE.md` first. For an object of uncertain identity,
  list two or three plausible readings (the mundane first) and what each predicts, check its size in human terms, and
  ask the owner "what is this?" rather than invent geometry. Model only what is observed: no extents nobody has seen,
  and a placeholder stays a placeholder until it is identified.
- Before asking the owner where something is, crop the photos and decide from them; ask only what they cannot show.
- Never add or shape geometry, or loosen a check, just to make a check pass. When a check fails after a photo-backed
  change, fix the geometry the check points at and keep the photo reading.

### Working with the owner

- Pairing order: edit, look (seconds), tell the owner what to look at in SketchUp. Tests, commits and doc updates come
  after, once the owner says the idea is a keeper.
- A small change the owner asks for directly (a colour, a finish, one number) needs no tests: make it, look, and say it
  is on screen.
- While the owner watches SketchUp, send no pictures of each change; they see it live. Pictures are for when they are
  away: `client.py views --view NAME` draws a design view (each run writes its own folder, so views sit side by side),
  and `overlay.gallery` the photos with today's house over them.
- Translate the owner's directions ("move it north", "the street side") through the compass table in `docs/HOUSE.md`
  before moving anything, and say the result in their words.
- In design work, one named constant per decision with the owner's words beside it. A rule a layout must keep (an
  aisle, a clearance) refuses at compile time with a stable code (docs/DESIGN.md). Derive ergonomic dimensions from the
  people who will use the space and from the appliances' own geometry, not from today's fixtures, which were someone
  else's choices.
- Start a new idea coarse (a plan, a massing) and add detail only once the owner picks it.
- Commit when the owner asks, or once they call something a keeper; the commit message carries the history.

### Speed

- A sync costs the entities it adds and erases. With a renderer extension such as Enscape open, each costs about 20
  times as much, so a whole rebuild can take a minute where an in-place sync of a design edit takes seconds.
  `look.py` syncs in place; an edit to `build/build.rb` rebuilds everything once (and the next look of each photo
  renders it again); `--rebuild` forces it.
- Run the full test suite when changing the tooling, not for each house edit. A house edit's check is the compile, the
  plan and a look.

## Extending the IR

When the house needs something the contract cannot say (a new field, a new element type), extend it end to end:

1. `docs/IR.md` first: the field or type, its units, default, meaning, and what refuses.
2. `lib/geometry.star`: the constructor takes it and checks it, failing with a message that names the element. A new
   unit field also joins `_check_unit`'s list of keys; a new type joins `TYPES`.
3. `ir/plan.py`: every rule `build.rb` would trip over is a refusal here; fill the default, resolve what the drawing
   code needs, and list the field where unknown fields are caught (`KNOWN` for an element's, `OPENING_KEYS`,
   `UNIT_KEYS` or `CASING_KEYS` for an opening's, a unit's or a casing's), or it warns as unknown. A new type also
   goes in `TYPES`, `PLANNERS`, `DEFAULT_FINISH`, and `ir/geom.py`'s `element_zrange`.
4. `build/build.rb`: draw from the planned element alone, and check each solid's volume. A new type also goes in its
   `TYPES` (the `type/<type>` tag) and a `build_<type>` method.
5. A fixture in `build/fixtures/` that uses it, and a broken case in `refused.json` (its note starts `refused: ` and
   names words of the message). `tests/test_plan.py` runs every fixture; `bridge/smoke.py` builds them in SketchUp.
6. Tests: `tests/test_geometry.py` (the constructor), `tests/test_plan.py` (defaults and refusals), `tests/test_ruby.py`
   with `tests/ruby/build_cases.rb` (what of build.rb runs without SketchUp).
7. If a pick should aim at it, its selectors in `overlay/features.py` (with `tests/test_features.py` and the table in
   docs/PHOTOS.md); if it shows in plan or elevation, `tools/plancheck.py`.
8. Run `uv run pytest -q`, then ask the owner before running `uv run python bridge/smoke.py`.

## Tests

- `uv run pytest -q` runs everything in about 15 seconds, without SketchUp. Run it after changing housec,
  `lib/geometry.star`, `lib/units.star`, `lib/checks.star`, `ir/`, `build/`, `bridge/`, `overlay/`, `extract/` or
  `tools/`; one file with `uv run pytest tests/test_plan.py`.
- The Ruby tests need Ruby 3.2 or later first on the PATH and skip without it (README.md, "What you need"; macOS's own
  Ruby is older). `ruby -wc build/build.rb` checks syntax.
- Tests of the cottage's own numbers skip once `house.json`'s project name is not "Example cottage". The rest hold any
  house: every element plans, every element can be named by a pick, the design areas' manifest shows exactly each
  combination's house.
- A design's rules get a test that moves one constant past the rule and expects the rule's code (docs/DESIGN.md,
  "Testing a design").
- `uv run python bridge/smoke.py` is the live check of `build.rb` and `render.rb` in the open SketchUp: a few seconds,
  drawn into a throwaway group. Run it after changing either file, after asking the owner.

## SketchUp etiquette

- SketchUp is the owner's working window. Every bridge command runs on its main thread and freezes it while it runs.
  Keep to short looks; ask before anything longer than about ten seconds (`bridge/smoke.py`, `look.py --rebuild` with
  a renderer open, re-matching many photos, a run of renders).
- When the owner says SketchUp is theirs, send nothing until they hand it back.
- Never quit, stop or signal SketchUp or its extensions. If a request is not picked up or SketchUp hangs, tell the
  owner; a modal dialog or a group open for editing is the usual cause (bridge/README.md, Troubleshooting).
- Change the model only through `look.py` and `bridge/furnishings.py`; renders leave it as it was. `client.py eval`
  is for reading the model. `client.py build` is for fixtures and always takes `--top NAME`, since its default top
  group is `HOUSE`.
- A sync keeps the owner's tag and tag folder visibility, active tag, camera, HOUSE's own tag and the other top-level
  groups, and `look.py` checks all of these but the camera around it. If it warns, tell the owner what changed rather
  than putting it back yourself.
- Save the model (`client.py save`) only when the owner asks.

## Writing

- Docs, `docs/HOUSE.md` and code comments state current facts and standing rules, readable top to bottom by someone
  with no history. History belongs in commit messages.
- A constant's comment says where its number comes from. A comment in code explains why, briefly.
- To the owner: what to look at and where, in their words and their compass, briefly.
- Plain words, short sentences, no emoji.
