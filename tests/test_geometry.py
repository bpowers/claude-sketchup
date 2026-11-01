"""The Starlark libraries: lib/geometry.star's constructors (good input, and refusals that name the element),
lib/units.star's dimensions and chains, lib/frame.star's sheet registration, and lib/checks.star's closures."""
from __future__ import annotations

from pathlib import Path

import pytest

import housec

ROOT = Path(__file__).resolve().parent.parent
LOADS = ('load("lib/geometry.star", "box", "beam", "casing", "column", "door_seams", "face_finishes", "glazed_unit", '
         '"leaf_unit", "opening", "ortho_union", "rect", "roof_plane", "slab", "space", "split_lights", "stair", '
         '"swing", "wall", "zone", "check_zones", "check_unique_ids", "along", "hinge_jamb", "swing_side")\n'
         'load("lib/units.star", "inches", "fmt", "chain", "equal_bays", "solve_glass", "sqrt")\n'
         'load("lib/frame.star", "registration", "sheet_to_model")\n'
         'load("lib/checks.star", "stair_joins", "column_bears", "sills_on_floors")\n')


def star(tmp_path, body):
    """The value of the last expression of `body`, evaluated with the libraries loaded as a zone loads them."""
    entry = tmp_path / "entry.star"
    entry.write_text(LOADS + body)
    return housec._eval(housec.inline(entry, ROOT), "entry.star")


def refused(tmp_path, body):
    with pytest.raises(housec.HousecError) as e:
        star(tmp_path, body)
    return str(e.value)


WALL = ('wall("t.wall.a", "room", [0.0, 0.0], [120.0, 0.0], 6.0, 0.0, 96.0, kind = "exterior", openings = [%s], '
        'src = "assumed")')


# ------------------------------------------------------------------ constructors, good input

def test_a_wall_carries_its_openings_in_order_with_their_units_and_casings(tmp_path):
    w = star(tmp_path, WALL % (
        'opening("t.wall.a.window", "window", 60.0, 36.0, 30.0, 80.0, casing = casing(faces = "+y"), '
        'unit = glazed_unit(verticals = split_lights(2.0, 34.0, 2, 1.5), mullion = 1.5)), '
        'opening("t.wall.a.door", "door", 10.0, 32.0, 0.0, 80.0, swing = swing("start", "left"), '
        'unit = leaf_unit(leaf_finish = "paint_white"))') + "\n")
    assert w["id"] == "t.wall.a" and w["type"] == "wall" and w["zone"] == "room" and w["src"] == "assumed"
    assert w["top_a"] == w["top_b"] == 96.0 and w["kind"] == "exterior"
    assert [o["id"] for o in w["openings"]] == ["t.wall.a.door", "t.wall.a.window"]
    door, window = w["openings"]
    assert door["swing"] == {"hinge": "start", "side": "left"} and door["unit"]["glazed"] is False
    assert window["unit"]["verticals"] == [18.0] and window["unit"]["frame"] == 2.0
    assert window["casing"] == {"width": 3.5, "proud": 0.75, "faces": "left"}      # +y is left of a -> b along +x


def test_a_casing_takes_a_width_per_side(tmp_path):
    """A window running to its wall's end has no room for a board there: that side's width is 0, and the planner
    plans it."""
    w = star(tmp_path, WALL % ('opening("t.wall.a.window", "window", 84.0, 36.0, 30.0, 80.0, casing = casing(width = '
                               '{"left": 3.5, "right": 0, "bottom": 3.5, "top": 3.5}))'))
    assert w["openings"][0]["casing"]["width"] == {"left": 3.5, "right": 0.0, "bottom": 3.5, "top": 3.5}
    from ir.plan import plan_document
    p = plan_document({"ir_version": 1, "meta": {}, "zones": {"room": {"storey": 0}}, "datums": {}, "elements": [w]})
    assert p.ok, p.message()
    assert p.elements[0]["openings"][0]["casing"]["outer"] == [80.5, 120.0, 26.5, 83.5]


def test_a_drawn_door_becomes_at_hinge_and_side(tmp_path):
    """A partition drawn from y 6 up to y 306 (a -> b runs +y), its door's leaf hung at the jamb at y 48 and swinging
    toward +x: at is 42 in along the wall, the hinge the start jamb, the side the right of a -> b."""
    got = star(tmp_path, 'a, b = [270.0, 6.0], [270.0, 306.0]\n'
                         '[along(a, b, [0.0, 48.0]), hinge_jamb(a, b, 42.0, 30.0, [270.0, 48.0]), '
                         'swing_side(a, b, [300.0, 60.0])]')
    assert got == [42.0, "start", "right"]


def test_face_finishes_name_the_faces_by_where_they_look(tmp_path):
    got = star(tmp_path, '[face_finishes("t.wall.a", [0.0, 0.0], [10.0, 0.0], {"-y": "siding", "+y": "drywall"}), '
                         'face_finishes("t.wall.a", [10.0, 0.0], [0.0, 0.0], {"-y": "siding", "+y": "drywall"})]\n')
    assert got == [["drywall", "siding"], ["siding", "drywall"]]


def test_the_other_types_take_what_the_contract_states(tmp_path):
    els = star(tmp_path, """[
    space("t.space.a", "room", rect(0.0, 0.0, 100.0, 80.0), 0.0, ceiling_z = 96.0, floor_finish = "oak",
          src = "assumed"),
    space("t.space.porch", "porch", rect(0.0, -40.0, 100.0, 0.0), -14.0, build_floor = False, src = "assumed"),
    slab("t.slab.a", "porch", rect(0.0, -40.0, 100.0, 0.0), -14.0, 4.0, src = "assumed"),
    column("t.column.a", "porch", [10.0, -30.0], -14.0, 84.0, size = [5.5, 5.5],
           capital = {"size": [7.5, 7.5], "height": 3.5}, src = "assumed"),
    beam("t.beam.a", "porch", [10.0, 0.0], [10.0, -40.0], 5.5, 9.25, 84.0, src = "assumed"),
    stair("t.stair.a", "porch", [40.0, 0.0], [80.0, 0.0], [0.0, -1.0], 0.0, 2, 7.0, 11.0, src = "assumed"),
    box("t.box.a", "room", [0.0, 56.0, 0.0, 72.0, 80.0, 34.5], "base_cabinet", front = "-y",
        seams = door_seams(72.0, 24.0), rails = [28.5], src = "assumed"),
    roof_plane("t.roof.a", "roof", [[0.0, 0.0, 100.0], [100.0, 0.0, 100.0], [100.0, 40.0, 120.0],
                                   [0.0, 40.0, 120.0]], 8.0, holes = [[40.0, 10.0, 60.0, 30.0]], src = "assumed"),
]
""")
    by = {e["id"]: e for e in els}
    assert by["t.space.a"]["build_ceiling"] is True and by["t.space.a"]["ceiling_z"] == 96.0
    assert by["t.space.porch"]["build_floor"] is False and "build_ceiling" not in by["t.space.porch"]
    assert by["t.column.a"]["capital"] == {"size": [7.5, 7.5], "height": 3.5}
    assert by["t.stair.a"]["risers"] == 2 and by["t.box.a"]["seams"] == [24.0, 48.0]
    assert by["t.roof.a"]["holes"] == [[40.0, 10.0, 60.0, 30.0]]
    assert [e["type"] for e in els] == ["space", "space", "slab", "column", "beam", "stair", "box", "roof_plane"]


def test_the_planner_plans_what_the_constructors_build(tmp_path):
    """What lib/geometry.star accepts, ir/plan.py plans: the cottage's elements plan with no refusal."""
    from ir import plan
    doc = housec.compile_house(ROOT / "house.star")
    p = plan.plan_document(doc)
    assert p.ok, p.message()
    assert not [w for w in p.warnings if "unknown" in w.message], p.warnings


def test_ortho_union_outlines_an_l_shaped_room(tmp_path):
    got = star(tmp_path, 'ortho_union("t.space.l", [rect(0.0, 0.0, 100.0, 50.0), rect(0.0, 50.0, 40.0, 90.0)])\n')
    assert got == [[0.0, 0.0], [100.0, 0.0], [100.0, 50.0], [40.0, 50.0], [40.0, 90.0], [0.0, 90.0]]


def test_zones_are_declared_and_every_element_names_one(tmp_path):
    assert star(tmp_path, '[zone(0), zone(0, outdoor = True), zone(9)]\n') == [
        {"storey": 0}, {"storey": 0, "outdoor": True}, {"storey": 9}]
    says = refused(tmp_path, 'check_zones({"room": zone(0)}, [box("t.box.a", "attic", [0, 0, 0, 1, 1, 1], "trim", '
                             'src = "assumed")])\n')
    assert 't.box.a: zone "attic" is not declared' in says


# ------------------------------------------------------------------ constructors, refusals naming the element

@pytest.mark.parametrize("body, says", [
    (WALL % 'opening("t.wall.a.door", "door", 100.0, 32.0, 0.0, 80.0)', "t.wall.a: opening t.wall.a.door at 100.0"),
    (WALL % 'opening("t.wall.a.one", "window", 10.0, 40.0, 30.0, 80.0), '
            'opening("t.wall.a.two", "window", 40.0, 30.0, 30.0, 80.0)', "t.wall.a.two at 40.0 overlaps"),
    (WALL % 'opening("t.wall.a.door", "door", 10.0, 32.0, 0.0, 100.0)', "t.wall.a.door's head 100.0 is above"),
    (WALL % 'opening("t.wall.a.pass", "pass", 10.0, 32.0, 0.0, 80.0, casing = casing())',
     "t.wall.a.pass: a pass opening takes no casing"),
    (WALL % 'opening("t.wall.a.win", "window", 10.0, 32.0, 30.0, 80.0, swing = swing("start", "left"))',
     "t.wall.a.win: only doors swing"),
    (WALL % 'opening("t.wall.a.win", "window", 10.0, 32.0, 30.0, 80.0, unit = glazed_unit(verticals = [2.5], '
            'mullion = 1.5))', "t.wall.a.win: the unit's mullion at 2.5 leaves no light"),
    (WALL % 'opening("t.wall.a.win", "window", 10.0, 32.0, 30.0, 80.0, unit = glazed_unit(depth = 8.0))',
     "t.wall.a: the unit of t.wall.a.win, 8.0 deep"),
    ('wall("t.wall.a", "room", [0.0, 0.0], [120.0, 0.0], 6.0, 96.0, 96.0, src = "assumed")',
     "t.wall.a: z0 96.0 must be below"),
    ('wall("t.wall.a", "room", [0.0, 0.0], [120.0, 0.0], 6.0, 0.0, 96.0, kind = "brick", src = "assumed")',
     't.wall.a: unknown wall kind "brick"'),
    ('space("t.space.a", "room", [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0]], 0.0, src = "assumed")',
     "t.space.a: the outline must run counter-clockwise"),
    ('space("t.space.a", "room", rect(0.0, 0.0, 10.0, 10.0), 0.0, build_ceiling = True, src = "assumed")',
     "t.space.a: build_ceiling needs a ceiling_z"),
    ('box("t.box.a", "room", [0.0, 0.0, 0.0, 30.0, 24.0, 34.5], "base_cabinet", front = "-y", seams = [31.0], '
     'src = "assumed")',
     "t.box.a: a seam at 31.0 lies outside the 30.0 wide front"),
    ('box("t.box.a", "room", [0.0, 0.0, 0.0, 30.0, 24.0, 34.5], "cabinet", src = "assumed")',
     't.box.a: unknown box kind "cabinet"'),
    ('stair("t.stair.a", "room", [0.0, 0.0], [40.0, 0.0], [1.0, 0.0], 0.0, 2, 7.0, 11.0, src = "assumed")',
     "t.stair.a: descend [1.0, 0.0] must be square"),
    ('roof_plane("t.roof.a", "roof", [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 5.0], [0.0, 10.0, 0.0]], 8.0, '
     'src = "assumed")',
     "t.roof.a: point [0.0, 0.0, 0.0] is 1.18 in off the roof plane's best plane"),
    ('roof_plane("t.roof.a", "roof", [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 10.0, 0.06]], 8.0, '
     'src = "assumed")',
     "t.roof.a: point [0.0, 0.0, 0.0] is -0.01 in off"),
    (WALL % 'opening("t.wall.a.window", "window", 60.0, 36.0, 30.0, 80.0, casing = casing(width = {"left": 3.5}))',
     "casing: width as an object names exactly"),
    (WALL % 'opening("t.wall.a.window", "window", 60.0, 36.0, 30.0, 80.0, casing = casing(width = {"left": 0, '
     '"right": 0, "bottom": 0, "top": 0}))', "casing: every side of width is 0"),
    ('column("t.column.a", "room", [0.0, 0.0], 0.0, 96.0, size = [4.0, 4.0], capital = {"size": [3.0, 3.0], '
     '"height": 2.0}, src = "assumed")', "t.column.a: capital [3.0, 3.0] is smaller"),
    ('beam("t.beam.a", "room", [0.0, 0.0], [0.0, 0.1], 4.0, 8.0, 90.0, src = "assumed")',
     "t.beam.a: beam a and b nearly coincide"),
    ('box("Kitchen.Box", "room", [0, 0, 0, 1, 1, 1], "trim", src = "assumed")', 'element id "Kitchen.Box"'),
    ('box("t.box.a", "room", [0, 0, 0, 1, 1, 1], "trim", src = "guess")', 't.box.a: unknown src "guess"'),
    ('box("t.box.a", "room", [0, 0, 0, 1, 1, 1], "trim")', "t.box.a: give its src"),
    ('box("t.box.a", "room", [0, 0, 0, 1, 1, 1], "trim", finish = 3, src = "assumed")',
     "t.box.a: a finish is a palette key"),
    ('check_unique_ids([box("t.box.a", "room", [0, 0, 0, 1, 1, 1], "trim", src = "assumed"), '
     'box("t.box.a", "room", [0, 0, 0, 1, 1, 1], "trim", src = "assumed")])', 'duplicate id "t.box.a"'),
])
def test_a_bad_value_is_refused_naming_its_element(tmp_path, body, says):
    assert says in refused(tmp_path, body + "\n")


# ------------------------------------------------------------------ units

@pytest.mark.parametrize("text, want", [
    ('"12\'-6 1/2\\""', 150.5), ('"6\'-8\\""', 80.0), ('"3 1/2\\""', 3.5), ('"7\'"', 84.0), ("10", 10.0),
    ('"0\'-3/4\\""', 0.75), ('"-2\'-0\\""', -24.0), ('"30\'-0\\""', 360.0),
])
def test_inches_parses_feet_and_inches_as_drawings_write_them(tmp_path, text, want):
    assert star(tmp_path, f"inches({text})\n") == want


def test_fmt_writes_inches_back_as_feet_and_inches(tmp_path):
    assert star(tmp_path, '[fmt(150.5), fmt(80.0), fmt(-24.0), fmt(0.0625)]\n') == [
        "12'-6 1/2\"", "6'-8\"", "-2'-0\"", "0'-0 1/16\""]


def test_a_chain_gives_stations_and_solves_its_fill_from_the_overall(tmp_path):
    c = star(tmp_path, 'chain([["a", "10\'-0\\""], ["b", "FILL"], ["c", "4\'-6\\""]], start = 6.0, '
                       'closes = "24\'-0\\"")\n')
    assert c["stations"] == [6.0, 126.0, 240.0, 294.0] and c["filled"] == 114.0 and c["total"] == 288.0
    assert [s["label"] for s in c["spans"]] == ["a", "b", "c"]


@pytest.mark.parametrize("body, says", [
    ('chain(["10\'-0\\"", "4\'-0\\""], closes = "14\'-1\\"", name = "front")',
     "front does not close: the run sums to 14'-0\" but the overall is 14'-1\" (off by -0'-1\")"),
    ('chain(["FILL", "FILL"], closes = "14\'-0\\"")', "only one FILL span"),
    ('chain(["FILL", "4\'-0\\""])', "a FILL span needs a stated overall"),
    ('chain(["10\'-0\\"", "FILL"], closes = "8\'-0\\"")', "already exceed the overall by 2'-0\""),
])
def test_a_chain_that_does_not_close_fails_the_build(tmp_path, body, says):
    assert says in refused(tmp_path, body + "\n")


def test_equal_lights_and_the_glass_a_chain_implies(tmp_path):
    bays, glass, root = star(tmp_path, '[equal_bays(3, 60.0, 1.5, jamb = 2.0), solve_glass([2.0, 2.0], [[3, 1.5]], '
                                       '60.0), sqrt(2.0)]\n')
    assert bays["glass"] == pytest.approx(17.666666, abs=1e-5) and len(bays["bays"]) == 3
    assert bays["bays"][2]["b"] == pytest.approx(58.0) and glass == pytest.approx(bays["glass"])
    assert root == pytest.approx(2.0 ** 0.5, abs=1e-12)
    assert star(tmp_path, "split_lights(2.0, 58.0, 3, 1.5)\n") == pytest.approx([20.416667, 39.583333], abs=1e-6)


# ------------------------------------------------------------------ frame: drawing sheets

def test_a_registration_maps_sheet_points_into_the_model(tmp_path):
    got = star(tmp_path, """[
    sheet_to_model(registration([102.5, 88.0], 2.0 / 3.0), [462.5, 88.0]),
    sheet_to_model(registration([100.0, 100.0], 1.0, 90.0), [110.0, 100.0]),
    sheet_to_model(registration([0.0, 0.0], 2.0, 30.0), [10.0, 0.0]),
]
""")
    assert got[0] == [240.0, 0.0] and got[1] == [0.0, 10.0]
    assert got[2] == [17.321, 10.0]          # 20 cos 30 deg, to the thousandth of an inch


# ------------------------------------------------------------------ checks across elements

PORCH = """[
    space("t.space.room", "room", rect(0.0, 6.0, 100.0, 100.0), 0.0, ceiling_z = 96.0, src = "assumed"),
    space("t.space.porch", "porch", rect(0.0, -40.0, 100.0, 0.0), -14.0, build_floor = False, src = "assumed"),
    wall("t.wall.front", "room", [0.0, 3.0], [100.0, 3.0], 6.0, -18.0, 104.0, kind = "exterior", openings = [
        opening("t.wall.front.door", "door", 30.0, 36.0, %s, 80.0)], src = "assumed"),
    slab("t.slab.porch", "porch", rect(0.0, -40.0, 100.0, 0.0), -14.0, 4.0, src = "assumed"),
    stair("t.stair.a", "porch", [30.0, 0.0], [66.0, 0.0], [0.0, -1.0], %s, 2, 7.0, 11.0, src = "assumed"),
    column("t.column.a", "porch", [90.0, -35.0], -14.0, 84.0, size = [5.5, 5.5], src = "assumed"),
    beam("t.beam.a", "porch", [90.0, 0.0], [90.0, -40.0], 5.5, 9.25, %s, src = "assumed"),
]
"""


def test_the_closures_hold_on_a_house_that_fits_together(tmp_path):
    lines = star(tmp_path, "E = " + PORCH % ("0.0", "0.0", "84.0") + (
        '[stair_joins(E, "t.stair.a", "t.space.room", "t.space.porch", top_probe = 9.0), '
        'column_bears(E, "t.column.a", "t.slab.porch", "t.beam.a"), sills_on_floors(E)]\n'))
    assert lines == ["t.stair.a joins t.space.room (z 0.0) to t.space.porch (z -14.0)",
                     "t.column.a stands on t.slab.porch and carries t.beam.a",
                     "door and cased sills: 1 stand on a floor beside them"]


@pytest.mark.parametrize("values, check, says", [
    (("0.0", "7.0", "84.0"), 'stair_joins(E, "t.stair.a", "t.space.room", "t.space.porch", top_probe = 9.0)',
     "t.stair.a: its top vs the floor of t.space.room: got 7.0"),
    (("0.0", "0.0", "84.0"), 'stair_joins(E, "t.stair.a", "t.space.room", "t.space.porch")',
     "t.stair.a: the top of the stair does not start from t.space.room"),
    (("0.0", "0.0", "86.0"), 'column_bears(E, "t.column.a", "t.slab.porch", "t.beam.a")',
     "t.column.a: its top vs the soffit of t.beam.a: got 84.0, expected 86.0"),
    (("8.0", "0.0", "84.0"), "sills_on_floors(E)", "t.wall.front.door: its sill 8.0 is neither within 0.5 in"),
])
def test_a_closure_that_does_not_hold_fails_naming_the_element(tmp_path, values, check, says):
    assert says in refused(tmp_path, "E = " + PORCH % values + check + "\n")
