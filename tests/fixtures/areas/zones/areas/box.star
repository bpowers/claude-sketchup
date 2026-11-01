# The fixture's one design helper, which every layer loads.

def fixture_box(eid, x0):
    return {"id": eid, "type": "box", "zone": "room", "src": "assumed", "kind": "trim",
            "bbox": [x0, 0.0, 0.0, x0 + 10.0, 10.0, 10.0]}
