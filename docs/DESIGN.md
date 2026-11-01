# Design areas: trying remodels

`house.star` is the house as it stands today, and `house.json` is what it compiles to. A remodel idea is never an
edit to today's house. It is an **alternative** of a **design area**, compiled beside today's house and switched on
and off in SketchUp by its own tag, so the owner can flip between today and each idea in the same model, from any
angle, while the photos keep matching today's house.

This page covers what an area is, what a design file looks like, how a change goes from the owner's words to
SketchUp, and how to add an alternative or an area. The example cottage has one area, `kitchen`, with two
alternatives, `island` and `peninsula`; the snippets below come from it, and stay as examples of the form after a new
house removes the cottage's area.

## Areas, alternatives and views

- An **area** is a part of the house the owner weighs on its own: the kitchen's layout, the bath, a wall that might
  come out. `zones/areas.star` lists the areas in `AREAS`, in the order their layers apply. A house with no
  `zones/areas.star` has no areas.
- An **alternative** of an area is one file, `zones/areas/<area>/<alt>.star`, called a **layer**: what it removes
  from the house, what it replaces and what it adds. Every area also has the alternative `today`, which changes
  nothing and has no file.
- A **view** is a named combination: one alternative for each area it names, `today` for the rest. `VIEWS` in
  `zones/areas.star` lists them. Each view compiles to a whole-house document of its own,
  `out/options/<view>.json`, which is what a design's tests read and what a render of the view shows.

```
{
    "AREAS": [
        {"id": "kitchen", "name": "Kitchen layout"},
    ],
    "VIEWS": {
        "kitchen_island": {"name": "The kitchen with a freestanding island and no uppers",
                           "choose": {"kitchen": "island"}},
        ...
    },
}
```

Ids of areas, alternatives and views are lowercase letters, digits and `_`, starting with a letter. An alternative's
id is its file's name.

## What shows in SketchUp

Each alternative is a tag, `<area>/<alt>`, and each area's `today` is one too: `kitchen/today`, `kitchen/island`,
`kitchen/peninsula`. A tag new to the model comes up shown when it is an area's today and hidden otherwise; after
that, a build never changes a tag the owner has set. The owner shows one tag per area, from the Tags panel or from
**Extensions > House twin > _area name_ > _alternative_**, which shows the one chosen and hides the area's others.
Keep an area's `name` short: it is that menu's label.

Nothing is built per combination. The top group `HOUSE` holds every piece once: today's elements and each
alternative's. `areasc.py` works out, for each piece, the **tag chains** it shows under, and the sync nests it in one
group per tag of each chain, so it shows only while every tag of the chain is on:

| Chain | What shows under it in the cottage |
|---|---|
| `[]` | the shell, everything no alternative touches: always shown |
| `["kitchen/today"]` | today's back run and refrigerator, which both alternatives replace |
| `["kitchen/today"]`, `["kitchen/peninsula"]` | today's uppers, which the island takes down and the peninsula keeps |
| `["kitchen/island"]`, `["kitchen/peninsula"]` | the moved refrigerator and the longer back run, the same in both: drawn once and shown under each tag |
| `["kitchen/island"]` | the island |

An area that reads another (below) can make chains of two tags, such as `["kitchen/island", "dining/bench"]`: a piece
that stands only with that island and that bench together. Whichever tag of each area is on, SketchUp shows exactly
the house that combination compiles to; `tests/test_areas.py` holds the manifest to that for every combination.

Renders made for photo overlays always show today's house: of each area's tags they turn on the area's `today`. A
render request's `areas` (a view's tags, from `out/areas/manifest.json`) draws that view instead, and
`uv run python bridge/client.py views --view kitchen_island` writes four overview pictures of a view to
`out/views/kitchen_island/`, beside today's in `out/views/today/`, whatever tags the owner has on.

## The change loop

1. The owner says what they want, in their words: "make the island six inches longer".
2. Change the one constant that decides it (`ISLAND_LENGTH` in `zones/areas/kitchen.star`), and keep the owner's
   words beside it.
3. Run `uv run python look.py`. It compiles the house and the areas, and syncs SketchUp: only the pieces whose keys
   changed are built, so a design edit reaches SketchUp in seconds.
4. The owner looks in SketchUp, switching the area's tags, and says what next.

Without SketchUp, compile to see what the design measured, or why it refuses:

```
uv run python look.py --compile-only         # the house, every view (out/options/<view>.json) and the manifest
uv run python housec.py house.star --option kitchen_island -o out/options/kitchen_island.json   # one view alone
```

`look.py --compile-only` compiles house.json from the sources first and prints what is unavailable; `areasc.py` alone
composes the house.json already on disk, so after a zone edit it composes the old house.

A view's `meta.option.checks` lists what the design measured, one line of data each (below); diff two compiles and
the numbers that moved show. `meta.option.base.sha256` and `meta.option.source_sha256` record the house and the
layers' sources it was compiled from. `house.json` is never changed by a design.

## A layer

A layer's last expression is `{"LAYER": {...}}`:

| Key | What it is |
|---|---|
| `id`, `name` | the alternative's id (its file's name) and what it is, in a few words |
| `removes` | ids of elements of the house the areas before it leave |
| `replaces` | whole elements, each keeping the id and type of one there: a refrigerator moved is a refrigerator replaced |
| `adds` | elements with ids the house does not use (openings share the id namespace) |
| `checks` | what its design measured, as check lines |
| `note`, `sources`, `later` | what it is in one line, what it was drawn from, what it leaves for later |
| `exports` | what it hands the areas that read it |
| `drops` | `{"checks": [codes], "later": [lines]}`: what an earlier layer reported that no longer holds |

`id`, `name`, `removes`, `replaces` and `adds` are required. housec puts the tag `<area>/<alt>` on every element a layer
replaces or adds.

Openings live in their wall, so an alternative that moves, adds, widens or removes an opening replaces the wall: the
same id, built with the zone's own wall function, with its new list of openings. A wall a layer takes out is removed,
and a new one is added under an id the house does not use.

A layer loads `lib/` and `zones/` modules the way a zone does, and builds from today's pieces: the cottage's kitchen
alternatives call `kitchen_base()`, `kitchen_counter()` and `kitchen_fridge()` from `zones/kitchen.star` and stand
on the room faces in `lib/frame.star`. So when a survey corrects a wall, the design moves with it.

Layers apply in the order of `AREAS`. A later layer may replace or remove what an earlier one added; adding an id
already in use, or touching an id an earlier layer removed, is refused. A later check line with the same code takes
the earlier one's place.

### Areas that read other areas

An area that names earlier areas in `reads` is fitted to whichever of their alternatives is on: its layers are given
the predeclared `UPSTREAM`, `{"<area it reads>": {"alt": "<that area's alternative>", ...what that layer exports}}`.
A bench fitted to the kitchen's peninsula reads `kitchen` and finds the peninsula's place in
`UPSTREAM["kitchen"]`.

A layer that cannot stand with what it reads refuses with a **choice code**:

```
fail("dining choice.no_peninsula: the bench stands along a peninsula; this kitchen has none")
```

Its area then stays as today in that combination: switching on its tag shows today's pieces of that area. The
compile says so (`dining/bench shows as dining/today with kitchen/island: choice.no_peninsula: ...`) and the manifest
lists it under `unavailable`. A view that names such a combination is an error, and so is an alternative that can
stand in no combination at all.

## The design file: one constant per decision

An area's alternatives are usually cut from one design file, `zones/areas/<area>.star`, which holds every number the
design decides as a named constant with the owner's words beside it, and a function that builds the layer from a few
choices. Each alternative is then a header and one call:

```
load("zones/areas/kitchen.star", "kitchen_layer")

# Kitchen alternative island: a freestanding island with stools on the living room side, the back run carried on to
# the refrigerator at the end wall, and the uppers taken down (zones/areas/kitchen.star holds the design).
#
# The owner: "could we try an island we can walk all the way round? And take the uppers down, so the back wall feels
# open."

{"LAYER": kitchen_layer({"id": "island", "name": "A freestanding island, the uppers taken down",
                         "block": "island", "uppers": False})}
```

and in the design file:

```
ISLAND_LENGTH = 84.0             # the island's counter. The owner: "about seven feet long"
ISLAND_END_WALKWAY = 48.0        # from the west wall to the island's end. The owner: "walk all the way round it"
```

The comment is the design record: why the number is what it is, in the owner's words, where a drawing or a product
sheet gives it, its sheet beside it. Change the number, keep the comment true.

All modules a layer loads are inlined into one namespace, and housec refuses a top-level name defined twice. Give a
design's names words no zone uses (`ISLAND_LENGTH`, not `LENGTH`).

## Rules that refuse, with stable codes

A design states the rules it must hold as constants too, with the reason:

```
AISLE_MIN = 42.0                 # aisle: the working aisle between two counters (kitchen-planning guidance, one cook)
WALKWAY_MIN = 36.0               # walkway: a way past a counter's end or behind its stools
```

and refuses a design that breaks one, naming the rule by a stable code:

```
def kitchen_fail(code, words):
    fail("kitchen %s: %s" % (code, words))
```

so that moving the island 6 in toward the back run (`BLOCK_WALKWAY = 46.0`) fails the compile with
`kitchen aisle: 38.75 in, under the 42.0 in minimum`.
Tests and readers match the code, never the words, so the words can be rewritten freely. A rule's refusal is not a
choice code: an alternative that stops working is an error in every combination, never quietly shown as today's.

What the design measured goes into the layer's `checks`, one line of data each, `<code> <value> <op> <limit> ok`:

```
aisle 44.75 >= 42.0 ok
walkway.stools 40.0 >= 36.0 ok
counter.length 84.0 - - ok
seats 3 - - ok
```

A line with `-` for its op and limit is a measurement with no rule. The cottage's kitchen holds these rules:

| Rule | Inches | Code |
|---|---|---|
| the working aisle between the back run and the island or peninsula | 42 | `aisle` |
| the walkway behind the stools | 36 | `walkway.stools` |
| the walkway past each free end | 36 | `walkway.west_end`, `walkway.east_end` |

## Adding an alternative

1. Copy an alternative's file under the new id: `zones/areas/kitchen/island_long.star`.
2. Change its `id` (the file's name), its `name`, the choices it passes, and its header with the owner's words. A
   variation the design file cannot express yet is a new choice there: add it to the design's choices with its
   default, and use it in the function.
3. To name the combination, add a line to `VIEWS`.
4. Run `look.py`. The new tag comes up hidden in SketchUp, beside the area's others in the menu.

## Adding an area

1. Add `{"id": "bath", "name": "Bath"}` to `AREAS` in `zones/areas.star`, after any area it reads, with
   `"reads": [...]` if its alternatives are fitted to an earlier area's.
2. Write the design file, `zones/areas/bath.star`: the constants with the owner's words, the rules, and a
   `bath_layer(choices)` that returns the layer.
3. Make `zones/areas/bath/` with one file per alternative.
4. Add views for the combinations worth naming, and run `look.py`.

Areas that reach no common element compile apart, so each one adds its alternatives to the compile's cost rather
than multiplying it.

## Testing a design

`tests/test_areas.py` holds the manifest to the promise above for every combination; past 128 combinations, for every
combination within each group of areas that compile together, and a sample of those across groups. A design's rules are
tested by moving one of its constants past the rule and asserting the code:

```
HOUSE = ROOT / "house.star"                  # ROOT: the repository, Path(__file__).resolve().parent.parent
design = housec.Design(HOUSE, rewrite=lambda src: src.replace("ISLAND_LENGTH = 84.0", "ISLAND_LENGTH = 150.0"))
with pytest.raises(housec.HousecError, match="kitchen walkway.east_end:"):
    housec.evaluate_option(HOUSE, "kitchen_island", design=design)
```

`tests/test_housec.py` has the cottage's version of this.

Loose furniture (a table, a sofa) is not a design area: it lives in the `FURNISHINGS` group, placed from
`furnishings/pieces.json` (`furnishings/README.md`).
