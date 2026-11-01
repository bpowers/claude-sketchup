load("zones/areas/box.star", "fixture_box")

# p stands where a's alternative says, takes out what x added and box four, and has no place to stand in today's a.

def p():
    at = UPSTREAM["a"].get("at")
    if at == None:
        fail("fixture choice.no_a: p stands in x or y")
    gone = ["a.box.new"] if UPSTREAM["a"]["alt"] == "x" else []
    return {"id": "p", "name": "p", "removes": gone + ["room.box.four"], "replaces": [],
            "adds": [fixture_box("b.box.new", at + 20.0)], "checks": ["width 12.0 - - ok"],
            "drops": {"later": ["the trim"]}}

{"LAYER": p()}
