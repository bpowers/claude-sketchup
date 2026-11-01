load("zones/areas/box.star", "fixture_box")

# x removes box one, moves box two, adds a box of its own, and tells b where to stand.

{"LAYER": {"id": "x", "name": "x", "removes": ["room.box.one"], "replaces": [fixture_box("room.box.two", 25.0)],
           "adds": [fixture_box("a.box.new", 100.0)], "exports": {"at": 100.0}, "checks": ["width 10.0 - - ok"],
           "later": ["the trim"]}}
