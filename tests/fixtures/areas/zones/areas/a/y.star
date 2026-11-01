load("zones/areas/box.star", "fixture_box")

# y adds the very box x adds, and tells b to stand elsewhere.

{"LAYER": {"id": "y", "name": "y", "removes": [], "replaces": [], "adds": [fixture_box("a.box.new", 100.0)],
           "exports": {"at": 200.0}}}
