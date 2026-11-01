"""The planner (ir/plan.py): every fixture plans or refuses as its notes say, defaults are filled in, messages name
their elements, and the plan file is what build.rb reads.

    uv run pytest tests/test_plan.py
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ir import plan
from ir.plan import DOCUMENT, plan_document

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "build" / "fixtures"
REFUSED = FIXTURES / "refused.json"
BUILDS = sorted(p for p in FIXTURES.glob("*.json") if p != REFUSED)


def load(path):
    return json.loads(Path(path).read_text())


def doc(*elements, **top):
    """A minimal document around the elements."""
    d = {"ir_version": 1, "meta": {}, "zones": {"living": {"storey": 0}, "porch": {"storey": 0, "outdoor": True}},
         "datums": {}, "elements": list(elements)}
    d.update(top)
    return d


def wall(**kw):
    w = {"id": "living.wall.south", "type": "wall", "zone": "living", "src": "assumed", "a": [0, 0], "b": [120, 0],
         "thickness": 6.0, "z0": 0.0, "top_a": 96.0, "top_b": 96.0, "kind": "interior"}
    w.update(kw)
    return w


def opening(**kw):
    o = {"id": "living.wall.south.w", "kind": "window", "at": 20.0, "width": 40.0, "sill_z": 30.0, "head_z": 70.0}
    o.update(kw)
    return o


def one(d):
    p = plan_document(d)
    assert p.ok, p.message()
    return p, p.elements[0]


# ------------------------------------------------------------------ the fixtures

@pytest.mark.parametrize("path", BUILDS, ids=lambda p: p.name)
def test_every_fixture_but_the_refused_one_plans_cleanly(path):
    d = load(path)
    p = plan_document(d, str(path))
    assert p.ok, p.message()
    assert p.warnings == [], [str(w) for w in p.warnings]
    assert [e["id"] for e in p.elements] == [e["id"] for e in d["elements"]]
    json.dumps(p.elements, allow_nan=False)


def test_the_refused_fixture_refuses_every_element_for_the_reason_in_its_note():
    d = load(REFUSED)
    p = plan_document(d, str(REFUSED))
    assert p.elements == []
    by_id = {}
    for f in p.refusals:
        assert f.element_id not in by_id, f"two refusals for {f.element_id}"
        by_id[f.element_id] = f.message
    for e in d["elements"]:
        reason = e["note"].removeprefix("refused: ")
        assert e["note"].startswith("refused: ") and e["id"] in by_id, e["id"]
        assert reason in by_id[e["id"]], (e["id"], reason, by_id[e["id"]])


def test_a_planned_element_is_the_same_wherever_it_stands_in_the_document():
    """Content keys (areasc.py) hash planned elements, so nothing in one may depend on its place in the document."""
    d = load(FIXTURES / "walls.json")
    a = plan_document(d).elements
    d2 = copy.deepcopy(d)
    d2["elements"].reverse()
    b = {e["id"]: e for e in plan_document(d2).elements}
    assert all(json.dumps(e, sort_keys=True) == json.dumps(b[e["id"]], sort_keys=True) for e in a)


def test_whole_numbers_and_floats_plan_alike():
    as_ints = wall(a=[0, 0], b=[120, 0], thickness=6, z0=0, top_a=96, top_b=96)
    assert json.dumps(one(doc(as_ints))[1]) == json.dumps(one(doc(wall()))[1])


# ------------------------------------------------------------------ defaults

def test_wall_defaults():
    _, w = one(doc(wall(kind="exterior", finish_left="drywall")))
    assert w["length"] == 120.0 and w["openings"] == []
    assert (w["finish"], w["finish_left"], w["finish_right"]) == ("siding", "drywall", "siding")
    _, w = one(doc(wall(kind="masonry")))
    assert w["finish"] == w["finish_left"] == w["finish_right"] == "brick"


def test_a_number_frame_has_no_threshold_member_on_a_door_and_glazing_follows_the_kind():
    _, w = one(doc(wall(openings=[opening(unit={"frame": 2.0}),
                                  opening(id="living.wall.south.d", kind="door", at=70.0, width=36.0, sill_z=0.0,
                                          head_z=80.0, unit={"frame": 2.0})])))
    win, door = w["openings"]
    assert win["unit"]["frame"] == {"left": 2.0, "right": 2.0, "bottom": 2.0, "top": 2.0} and win["unit"]["glazed"]
    assert door["unit"]["frame"] == {"left": 2.0, "right": 2.0, "bottom": 0.0, "top": 2.0}
    assert door["unit"]["glazed"] is False and door["unit"]["lights"] == [[72.0, 104.0, 0.0, 78.0]]
    assert win["unit"]["depth"] == 3.5 and win["unit"]["offset"] == 0.0
    assert (win["unit"]["finish"], win["unit"]["leaf_finish"]) == ("trim_white", "wood_mid")


def test_a_unit_on_a_thin_wall_is_as_deep_as_the_wall():
    _, w = one(doc(wall(thickness=2.0, openings=[opening(unit={"frame": 1.0})])))
    assert w["openings"][0]["unit"]["depth"] == 2.0


def test_mullions_and_rails_split_the_lights():
    _, w = one(doc(wall(openings=[opening(unit={"frame": 2.0, "verticals": [20.0], "mullion": 2.0,
                                                "horizontals": [50.0], "rail": 2.0})])))
    assert w["openings"][0]["unit"]["lights"] == [
        [22.0, 39.0, 32.0, 49.0], [41.0, 58.0, 32.0, 49.0], [22.0, 39.0, 51.0, 68.0], [41.0, 58.0, 51.0, 68.0]]


def test_a_cased_opening_takes_the_default_casing_where_it_fits_and_warns_where_it_does_not():
    _, w = one(doc(wall(openings=[opening(kind="cased", sill_z=0.0, head_z=80.0)])))
    c = w["openings"][0]["casing"]
    assert c["outer"] == [16.5, 63.5, 0.0, 83.5] and c["faces"] == ["left", "right"] and c["finish"] == "trim_white"
    p, w = one(doc(wall(openings=[opening(kind="cased", at=1.0, sill_z=0.0, head_z=80.0)])))
    assert w["openings"][0]["casing"] is None
    assert [f.element_id for f in p.warnings] == ["living.wall.south.w"] and "no room" in p.warnings[0].message


def test_a_casing_width_object_names_each_side():
    _, w = one(doc(wall(openings=[opening(casing={"width": {"left": 3, "right": 3, "bottom": 0, "top": 4},
                                                  "faces": "right", "proud": 1.0})])))
    c = w["openings"][0]["casing"]
    assert c["outer"] == [17.0, 63.0, 30.0, 74.0] and c["faces"] == ["right"] and c["proud"] == 1.0


def test_opening_edges_within_tolerance_snap():
    _, w = one(doc(wall(openings=[opening(at=0.0004, width=39.9996, sill_z=0.0004, head_z=95.9996)])))
    assert w["openings"][0]["rect"] == [0.0, 40.0, 0.0, 96.0]


def test_space_defaults():
    sp = {"id": "living.space.main", "type": "space", "zone": "living", "src": "cad",
          "outline": [[0, 0], [100, 0], [100, 80], [0, 80]], "floor_z": 0.0}
    _, s = one(doc(sp))
    assert s["build_floor"] and not s["build_ceiling"] and s["ceiling_z"] is None and s["ceiling_plane"] is None
    _, s = one(doc(dict(sp, ceiling_z=96.0)))
    assert s["build_ceiling"] and (s["floor_finish"], s["ceiling_finish"]) == ("oak", "drywall")
    _, s = one(doc(dict(sp, ceiling_plane={"at": [0, 0, 120], "slope": [0, -0.25]})))
    assert s["build_ceiling"] and s["ceiling_plane"] == {"at": [0.0, 0.0, 120.0], "slope": [0.0, -0.25]}


def test_a_repeated_closing_point_and_a_corner_on_a_straight_run_are_dropped():
    sp = {"id": "living.space.main", "type": "space", "zone": "living", "src": "cad", "floor_z": 0.0,
          "outline": [[0, 0], [50, 0], [100, 0], [100, 80], [0, 80], [0, 0]]}
    assert one(doc(sp))[1]["outline"] == [[0.0, 0.0], [100.0, 0.0], [100.0, 80.0], [0.0, 80.0]]


def test_other_defaults():
    els = [{"id": "living.box.a", "type": "box", "zone": "living", "src": "photo", "bbox": [0, 0, 0, 10, 10, 10],
            "kind": "counter"},
           {"id": "living.column.a", "type": "column", "zone": "living", "src": "cad", "center": [0, 0],
            "shape": "round", "diameter": 6, "z0": 0, "z1": 90},
           {"id": "living.beam.a", "type": "beam", "zone": "living", "src": "cad", "a": [0, 0], "b": [10, 0],
            "width": 4, "depth": 8, "soffit_z": 88, "kind": "faux"},
           {"id": "porch.slab.a", "type": "slab", "zone": "porch", "src": "cad",
            "outline": [[0, 0], [10, 0], [10, 10]], "z_top": 0, "thickness": 4},
           {"id": "living.roof.a", "type": "roof_plane", "zone": "living", "src": "cad",
            "pts": [[0, 0, 100], [10, 0, 100], [10, 10, 110]], "thickness": 6}]
    p = plan_document(doc(*els))
    assert p.ok, p.message()
    box, col, beam, slab, roof = p.elements
    assert (box["finish"], box["front"], box["seams"], box["subtype"]) == ("counter_stone", None, [], None)
    assert (col["size"], col["diameter"], col["capital"], col["finish"]) == (None, 6.0, None, "paint_white")
    assert (beam["finish"], beam["length"]) == ("wood_dark", 10.0)
    assert slab["finish"] == "concrete" and roof["holes"] == [] and roof["finish"] == "roof"


def test_every_default_finish_is_a_palette_key():
    pal = plan.load_palette()

    def values(d):
        for v in d.values():
            yield from values(v) if isinstance(v, dict) else [v]
    assert sorted(set(values(plan.DEFAULT_FINISH)) - set(pal)) == []
    assert "placeholder" in pal and pal["glass"]["alpha"] < 1


# ------------------------------------------------------------------ warnings

def test_an_unknown_finish_warns_and_draws_as_placeholder():
    p, w = one(doc(wall(finish="mauve_velvet")))
    assert w["finish"] == w["finish_left"] == "placeholder"
    assert [f.element_id for f in p.warnings] == ["living.wall.south"] and "mauve_velvet" in p.warnings[0].message


def test_an_unknown_field_warns():
    p, _ = one(doc(wall(thicknes=4.0, openings=[opening(unit={"frame": 1, "glazd": True})])))
    msgs = {f.element_id: f.message for f in p.warnings}
    assert "thicknes" in msgs["living.wall.south"] and "glazd" in msgs["living.wall.south.w"]


def test_a_door_whose_sill_stands_on_no_floor_warns():
    room = {"id": "living.space.main", "type": "space", "zone": "living", "src": "cad",
            "outline": [[0, 0], [120, 0], [120, 100], [0, 100]], "floor_z": 0.0}
    door = opening(id="living.wall.south.d", kind="door", sill_z=0.0, head_z=80.0)
    p, _ = one(doc(wall(z0=-14.0, openings=[door]), room))
    assert p.warnings == []
    p, _ = one(doc(wall(z0=-14.0, openings=[dict(door, sill_z=7.0)]), room))
    assert [f.element_id for f in p.warnings] == ["living.wall.south.d"]
    assert "living.space.main at 0" in p.warnings[0].message


# ------------------------------------------------------------------ refusals

def test_messages_name_the_element_and_the_opening():
    p = plan_document(doc(wall(openings=[opening(head_z=120.0)])))
    (f,) = p.refusals
    assert f.element_id == "living.wall.south" and f.message.startswith("opening living.wall.south.w: head_z 120")
    assert "living.wall.south: opening living.wall.south.w" in p.message()


def test_ids_are_unique_across_elements_and_openings():
    other = wall(id="living.wall.north", a=[0, 100], b=[120, 100], openings=[opening()])
    p = plan_document(doc(wall(openings=[opening()]), other))
    assert [f.element_id for f in p.refusals] == ["living.wall.north"]
    assert "duplicate id \"living.wall.south.w\": an opening of living.wall.south uses it first" in p.refusals[0].message
    p = plan_document(doc(wall(), wall()))
    assert [f.element_id for f in p.refusals] == ["living.wall.south"] and len(p.elements) == 1


def test_one_refusal_per_element_and_the_rest_still_plan():
    p = plan_document(doc(wall(thickness=-1), wall(id="living.wall.north", a=[0, 100], b=[120, 100])))
    assert [f.element_id for f in p.refusals] == ["living.wall.south"]
    assert [e["id"] for e in p.elements] == ["living.wall.north"] and not p.ok


@pytest.mark.parametrize("change, words", [
    ({"ir_version": 2}, "ir_version is 2"),
    ({"zones": {}}, "'zones' must declare"),
    ({"zones": {"Living Room": {"storey": 0}}}, "zone name"),
    ({"location": {"latitude": 95.0}}, "'latitude' must lie in -90..90"),
    ({"location": {"north_angle": 360.0}}, "'north_angle' must be at least 0 and less than 360"),
    ({"datums": {"main": "zero"}}, "'datums' must map names to numbers"),
    ({"elements": {}}, "'elements' must be a list"),
])
def test_document_level_refusals(change, words):
    p = plan_document(dict(doc(wall()), **change), "house.json")
    assert [f.element_id for f in p.refusals] == [DOCUMENT] and words in p.refusals[0].message, p.refusals
    assert p.refusals[0].message.startswith("house.json: ")


def test_location_is_planned_from_the_keys_it_gives():
    p = plan_document(doc(wall(), location={"north_angle": 12.5, "city": "Town", "elevation": 3}))
    assert p.ok and p.location == {"city": "Town", "north_angle": 12.5}
    assert any("elevation" in f.message for f in p.warnings)
    assert plan_document(doc(wall())).location is None


# ------------------------------------------------------------------ the plan file and the CLI

def test_write_plan(tmp_path):
    d = load(FIXTURES / "sample.json")
    p = plan_document(d)
    out = plan.write_plan(p, FIXTURES / "sample.json", "ab" * 32, tmp_path / "plans" / "sample.json")
    body = json.loads(out.read_text())
    assert set(body) == {"plan", "ir", "ir_sha256", "elements", "location", "zones", "warnings"}
    assert body["plan"] == plan.PLAN_VERSION and body["ir"] == str((FIXTURES / "sample.json").resolve())
    assert body["elements"] == p.elements and body["location"]["city"] == "Example Town"
    assert body["zones"]["porch"] == {"storey": 0.0, "outdoor": True}
    stamp = out.stat().st_mtime_ns
    os.utime(out, ns=(stamp - 10**9, stamp - 10**9))
    plan.write_plan(p, FIXTURES / "sample.json", "ab" * 32, out)      # the same plan: the file is left alone
    assert out.stat().st_mtime_ns == stamp - 10**9
    with pytest.raises(ValueError, match="refusing to write a plan"):
        plan.write_plan(plan_document(load(REFUSED)), REFUSED, "ab" * 32, tmp_path / "no.json")


def test_cli(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, "-m", "ir.plan", *map(str, args)], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)
    r = run(FIXTURES / "sample.json", "-o", tmp_path / "p.json")
    assert r.returncode == 0 and "12 elements planned, 0 refused" in r.stdout and (tmp_path / "p.json").is_file()
    r = run(REFUSED)
    assert r.returncode == 1 and "REFUSED refused.wall.5: opening refused.wall.5.w" in r.stdout


def test_the_examples_in_docs_ir_md_plan():
    """Every JSON example of an element or an opening in docs/IR.md plans as it stands, so the contract cannot drift
    from the planner. Examples with placeholders ([x, y, z]) are not JSON and are skipped."""
    import re
    text = (ROOT / "docs" / "IR.md").read_text()
    blocks = []
    for block in re.findall(r"```json\n(.*?)```", text, re.S):
        try:
            blocks.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    top = next(b for b in blocks if "ir_version" in b)
    els = [b for b in blocks if "type" in b]
    els += [wall(id="example.wall.host", a=[0, 3], b=[192, 3], top_a=97.5, top_b=97.5, openings=[b])
            for b in blocks if "kind" in b and "at" in b and "type" not in b]
    assert len(els) >= 9
    p = plan_document(dict(top, elements=els))
    assert p.ok, p.message()
    assert p.warnings == [], [str(w) for w in p.warnings]
