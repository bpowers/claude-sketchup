# Furnishings

Loose furniture, placed in SketchUp to try layouts in the model. It is not part of the house: it is not in `house.json`,
no build or sync touches it, and photo overlays never show it. It lives in its own top-level SketchUp group,
`FURNISHINGS`, each piece a group or component named `furn/<id>` on a tag `furnishings/<zone>`. The list,
`pieces.json`, may be empty (`{"pieces": []}`) until the owner wants furniture placed; after an edit to it, `place`
draws the change.

```
uv run python bridge/furnishings.py place              # draw every listed piece (or --only ID ...)
uv run python bridge/furnishings.py remove ID [ID ...] # take pieces out
uv run python bridge/furnishings.py list               # what FURNISHINGS holds, against the list
```

Each command is one undo step in SketchUp. `place` creates what is missing and brings each piece to its entry in
`pieces.json`. A piece the owner has moved, turned or scaled by hand in SketchUp stays where they put it, and `place`
names it; to have the list's place again, `remove` it and `place` it. To keep a hand arrangement, read the piece's
new position (`list`, or SketchUp's Entity Info) into `pieces.json`.

## A piece

```json
{"id": "living_sofa", "recipe": "sofa", "at": [66.0, 111.0, 0.0], "rotation": 0.0,
 "size": [84.0, 36.0, 32.0], "params": {"cushions": 3}, "finish": "fabric_gray",
 "tag": "furnishings/living", "note": "why it stands here"}
```

- `id`: lowercase letters, digits and `_`, unique in the list.
- `at`: the piece's origin in model inches. A recipe's origin is its plan centre on the floor.
- `rotation`: degrees counter-clockwise seen from above, about the origin. At 0 a recipe's width runs along +x, its
  depth along +y, and its front faces -y (a sofa's seat, a bed's foot). So 180 faces +y, 90 faces +x and -90 faces -x.
- `size`: `[width, depth, height]` in inches; the recipe's default when left out (`SIZES` in
  `bridge/furnishings.py`).
- `finish`: the main material, a key of `build/palette.json` or one of the fabrics in `bridge/furnishings.py`
  (`fabric_gray`, `fabric_linen`, `fabric_white`, `fabric_navy`, `fabric_green`, `fabric_rust`).
- `params`: the recipe's own settings, over its defaults (`RECIPES` in `bridge/furnishings.rb` lists them with their
  defaults). A param ending in `_finish` is a material (a chair's seat takes the chair's finish unless `seat_finish`
  names another); `cushions`, `pillows`, `arm_left` and `arm_right` are whole numbers; every other param is inches.
- `tag`: `furnishings/<zone>`, named for the zone of the house the piece stands in (`tests/test_furnishings.py` checks
  that each recipe piece lies inside a space of that zone). A new tag is created shown, in the tag folder
  "furnishings"; the owner hides and shows a room's furniture with it.
- `note`: free text.

## Recipes

| recipe | size | params |
| --- | --- | --- |
| `box` | any block: a dresser, a cabinet, a television | none |
| `table` | the top's plan and height | `top` thickness, `leg` section, `inset` of the legs from the edges |
| `chair` | the seat's plan; height is the back's top | `seat_height`, `seat` thickness, `leg`, `back` thickness, `back_drop` (the back panel's height), `seat_finish` |
| `sofa` | overall; height is the back cushions' top | `seat_height`, `arm_height`, `arm` width, `back` thickness, `back_cushion` depth, `lean`, `leg_height`, `base`, `cushions`, `arm_left`, `arm_right` (1 or 0: a sectional's module has one arm or none), `leg_finish` |
| `bed` | width and length; height is the headboard's top | `frame_height`, `mattress` thickness, `headboard` thickness, `pillows`, `bedding_finish` |
| `rug` | its plan; height is its thickness | none |

A recipe refuses sizes it cannot draw (arms taller than the back, a mattress over the headboard) before anything
changes in SketchUp, and names the piece.

## Components

A real product model, such as a 3D Warehouse download, is a component: save it as a `.skp` file in
`furnishings/components/` and list it with `"component": "furnishings/components/<name>.skp"` in place of `recipe`,
`size`, `params` and `finish`. It keeps its own size, materials, origin and axes, so check in SketchUp where its origin
is before choosing `at` and `rotation`.

## Adding a recipe

A recipe is a plain Ruby method in `bridge/furnishings.rb`'s `Geometry` module that returns parts, each a closed solid
of planar faces wound counter-clockwise seen from outside (`loft`, `box`, `circle` and `pillow` build them). Add its
defaults to `RECIPES` and its method to `DRAW`, add its param names to `RECIPES` in `bridge/furnishings.py`, and run
`uv run pytest tests/test_furnishings.py`: it checks that both files name the same params, that every part is a closed
solid turned outward, and that each recipe fills exactly the size it is given.
