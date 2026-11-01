# A house of four boxes in one room, for the design-area tests (tests/test_areas.py, tests/test_housec.py).

def _box(eid, x0):
    return {"id": eid, "type": "box", "zone": "room", "src": "assumed", "kind": "trim",
            "bbox": [x0, 0.0, 0.0, x0 + 10.0, 10.0, 10.0]}

{
    "ir_version": 1,
    "meta": {"project": {"name": "fixture"}, "variant": "today", "generated_by": "housec"},
    "zones": {"room": {"storey": 0}},
    "datums": {"floor": 0.0},
    "elements": [_box("room.box.one", 0.0), _box("room.box.two", 20.0), _box("room.box.three", 40.0),
                 _box("room.box.four", 60.0)],
}
