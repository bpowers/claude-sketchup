# The design areas: the parts of the house the owner weighs remodels of, each switched on its own (docs/DESIGN.md).
#
# An area has alternatives to today's. Each alternative is a file, zones/areas/<area>/<alternative>.star, whose last
# expression is {"LAYER": {...}}: what it removes from the house, replaces in it and adds to it. Every area also has the
# alternative `today`, which changes nothing and has no file. In SketchUp each alternative is a tag, <area>/<alt>, and
# the owner shows one tag per area.
#
# AREAS lists the areas in the order their layers apply, each with an id and a short name (SketchUp's menu shows it).
# An area may name earlier areas in `reads`: its layers are then given what those areas' alternatives export (the
# predeclared UPSTREAM), so it can be fitted to whichever is on.
#
# VIEWS are named combinations, one alternative per area named and today's for the rest: each compiles to a document of
# its own (out/options/<view>.json) and a set of tags a render can show. To add one, add a line.

{
    "AREAS": [
        {"id": "kitchen", "name": "Kitchen layout"},
    ],
    "VIEWS": {
        "kitchen_island": {"name": "The kitchen with a freestanding island and no uppers",
                           "choose": {"kitchen": "island"}},
        "kitchen_peninsula": {"name": "The kitchen with a peninsula off the west wall, today's uppers kept",
                              "choose": {"kitchen": "peninsula"}},
    },
}
