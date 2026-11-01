"""Feature references and the picks file, on a small hand-written house document."""
import json

import pytest

from overlay.features import FeatureError, House, Resolved, Surface, resolve, resolve_target, unit_grid
from overlay.picks import PicksError, parse, reresolve

DOC = {
    "ir_version": 1,
    "zones": {"living": {"storey": 0}, "upper": {"storey": 1}, "roof": {"storey": 9},
              "porch": {"storey": 0, "outdoor": True}},
    "elements": [
        {"id": "living.wall.north", "type": "wall", "zone": "living", "src": "cad", "a": [-3, 183], "b": [243, 183],
         "thickness": 6, "z0": 0, "top_a": 96, "top_b": 96, "kind": "exterior",
         "openings": [{"id": "living.window.north", "kind": "window", "at": 63, "width": 48, "sill_z": 36,
                       "head_z": 84, "unit": {"frame": 2, "verticals": [24], "mullion": 2, "horizontals": [60],
                                              "rail": 1.5, "glazed": True, "depth": 3, "offset": 1}}]},
        {"id": "living.wall.east", "type": "wall", "zone": "living", "src": "cad", "a": [243, -3], "b": [243, 183],
         "thickness": 6, "z0": 0, "top_a": 96, "top_b": 96, "kind": "exterior",
         "openings": [{"id": "living.door.east", "kind": "door", "at": 50, "width": 36, "sill_z": 0, "head_z": 80,
                       "unit": {"frame": 1.5, "glazed": False}},
                      {"id": "living.pass.east", "kind": "pass", "at": 120, "width": 30, "sill_z": 36, "head_z": 80}]},
        {"id": "living.wall.west", "type": "wall", "zone": "living", "src": "photo", "a": [-3, -3], "b": [-3, 183],
         "thickness": 6, "z0": 0, "top_a": 96, "top_b": 120, "kind": "exterior", "openings": []},
        {"id": "living.wall.bay", "type": "wall", "zone": "living", "src": "assumed", "a": [0, 0], "b": [40, 30],
         "thickness": 4, "z0": 0, "top_a": 96, "top_b": 96, "kind": "interior", "openings": []},
        {"id": "living.box.counter", "type": "box", "zone": "living", "src": "measured",
         "bbox": [150, 155, 0, 240, 180, 36], "kind": "counter"},
        {"id": "living.column.post", "type": "column", "zone": "living", "src": "cad", "center": [100, 100],
         "shape": "square", "size": [6, 6], "z0": 0, "z1": 96, "capital": {"size": [10, 10], "height": 6}},
        {"id": "living.column.round", "type": "column", "zone": "living", "src": "cad", "center": [60, 60],
         "shape": "round", "diameter": 8, "z0": 0, "z1": 96},
        {"id": "living.beam.main", "type": "beam", "zone": "living", "src": "cad", "a": [0, 90], "b": [240, 90],
         "width": 5, "depth": 9, "soffit_z": 87, "kind": "structural"},
        {"id": "porch.stair.front", "type": "stair", "zone": "porch", "src": "cad", "a": [100, -6], "b": [148, -6],
         "descend": [0, -1], "z_top": 0, "risers": 2, "rise": 7, "run": 11},
        {"id": "living.space.main", "type": "space", "zone": "living", "src": "cad",
         "outline": [[0, 0], [240, 0], [240, 180], [0, 180]], "floor_z": 0,
         "ceiling_plane": {"at": [0, 0, 96], "slope": [0.1, 0.0]}},
        {"id": "living.space.flat", "type": "space", "zone": "living", "src": "cad",
         "outline": [[0, 200], [100, 200], [100, 300], [0, 300]], "floor_z": 0, "ceiling_z": 96},
        {"id": "porch.slab.main", "type": "slab", "zone": "porch", "src": "cad",
         "outline": [[80, -60], [170, -60], [170, -6], [80, -6]], "z_top": -14, "thickness": 4},
        {"id": "roof.plane.south", "type": "roof_plane", "zone": "roof", "src": "photo",
         "pts": [[-12, -12, 100], [252, -12, 100], [252, 90, 150], [-12, 90, 150]], "thickness": 6},
    ]}


@pytest.fixture
def house():
    return House(json.loads(json.dumps(DOC)))


def m(house, ref, **kw):
    return pytest.approx(list(resolve(house, ref, **kw).model), abs=1e-9)


def test_walls_faces_ends_levels_and_corners(house):
    north = {"el": "living.wall.north"}
    assert resolve(house, [dict(north, face="-y", end="a", level="top")]).model == (-3.0, 180.0, 96.0)
    assert resolve(house, dict(north, face="left", end="b", level="base")).model == (243.0, 186.0, 0.0)
    # an inside corner: two walls' faces, a vertical line
    corner = resolve(house, [dict(north, face="-y"), {"el": "living.wall.east", "face": "-x"}], free="z")
    assert corner.model == (240.0, 180.0, None) and corner.free_axis == 2
    assert corner.elements == ("living.wall.north", "living.wall.east")
    assert corner.srcs == {"living.wall.north": "cad", "living.wall.east": "cad"}
    with pytest.raises(FeatureError, match="needs end a or b"):
        resolve(house, {"el": "living.wall.west", "face": "+x", "level": "top"}, free="y")
    assert resolve(house, {"el": "living.wall.west", "face": "+x", "end": "b", "level": "top"}).model == \
        (0.0, 183.0, 120.0)
    with pytest.raises(FeatureError, match="not a face of this element"):
        resolve(house, dict(north, face="+x", end="a"), free="z")


def test_openings_jambs_and_unit_lights(house):
    win = {"el": "living.window.north", "face": "-y"}
    assert resolve(house, dict(win, jamb="start", level="head")).model == (60.0, 180.0, 84.0)
    cols, rows = unit_grid(DOC["elements"][0]["openings"][0], 60.0, 108.0, 36.0, 84.0)
    assert cols == [(62.0, 83.0), (85.0, 106.0)] and rows == [(38.0, 59.25), (60.75, 82.0)]
    # a light's face is on the unit (3 in deep, centred 1 in toward the left of the wall's centreline y 183)
    assert m(house, dict(win, light=[0, 0], jamb="end", level="head")) == [83.0, 182.5, 59.25]
    # the mullion's centre is midway between the lights either side of it
    mullion = {"between": [dict(win, light=[0, 1], jamb="end", level="sill"),
                           dict(win, light=[1, 1], jamb="start", level="sill")]}
    assert m(house, mullion) == [84.0, 182.5, 60.75]
    with pytest.raises(FeatureError, match=r"light \[2, 0\] does not exist"):
        resolve(house, dict(win, light=[2, 0], jamb="end", level="head"))
    with pytest.raises(FeatureError, match="has no unit"):
        resolve(house, {"el": "living.pass.east", "face": "-x", "light": [0, 0], "jamb": "end", "level": "head"})
    assert resolve(house, {"el": "living.door.east", "face": "-x", "jamb": "end", "level": "head"}).model == \
        (240.0, 83.0, 80.0)


def test_casing_edges_stand_on_the_casings_front_plane():
    doc = json.loads(json.dumps(DOC))
    north = doc["elements"][0]["openings"][0]
    north["casing"] = {"width": {"left": 3, "right": 0, "bottom": 2, "top": 4}, "proud": 1, "faces": "right"}
    house = House(doc)
    win = {"el": "living.window.north", "face": "-y"}      # the wall's right face, y 180; the casing 1 in proud of it
    assert m(house, dict(win, casing="inner", jamb="start", level="head")) == [60.0, 179.0, 84.0]
    assert m(house, dict(win, casing="outer", jamb="start", level="head")) == [57.0, 179.0, 88.0]
    assert m(house, dict(win, casing="outer", jamb="end", level="sill")) == [108.0, 179.0, 34.0]   # no right board
    with pytest.raises(FeatureError, match="no casing on its left face"):
        resolve(house, {"el": "living.window.north", "face": "+y", "casing": "inner", "jamb": "end", "level": "head"})
    with pytest.raises(FeatureError, match="casing needs the face"):
        resolve(house, {"el": "living.window.north", "casing": "inner", "jamb": "end", "level": "head"})
    with pytest.raises(FeatureError, match="no casing"):          # a door without one
        resolve(house, {"el": "living.door.east", "face": "-x", "casing": "outer", "jamb": "end", "level": "head"})


def test_boxes_columns_beams_stairs(house):
    assert resolve(house, {"el": "living.box.counter", "x": "max", "y": "min", "z": "max"}).model == (240.0, 155.0, 36.0)
    assert resolve(house, {"el": "living.box.counter", "x": "centre", "y": "min"}, free="z").model == \
        (195.0, 155.0, None)
    post = {"el": "living.column.post"}
    assert resolve(house, dict(post, x="min", y="min", level="top")).model == (97.0, 97.0, 90.0)
    assert resolve(house, dict(post, part="capital", x="max", y="max", level="base")).model == (105.0, 105.0, 90.0)
    with pytest.raises(FeatureError, match="square section"):
        resolve(house, {"el": "living.column.round", "x": "min", "y": "centre"}, free="z")
    assert resolve(house, {"el": "living.beam.main", "side": "-y", "end": "b", "level": "soffit"}).model == \
        (240.0, 87.5, 87.0)
    assert resolve(house, {"el": "porch.stair.front", "riser": 1, "level": "foot", "end": "a"}).model == \
        (100.0, -17.0, -14.0)


def test_spaces_slabs_and_heights(house):
    space = {"el": "living.space.main"}
    assert resolve(house, dict(space, corner="+x+y", level="floor")).model == (240.0, 180.0, 0.0)
    assert resolve(house, dict(space, x="max", level="floor"), free="y").model == (240.0, None, 0.0)
    # a sloped ceiling is a height, taken where the rest of the reference pins x and y
    assert m(house, dict(space, corner="+x+y", level="ceiling")) == [240.0, 180.0, 120.0]
    assert m(house, [{"el": "living.wall.east", "face": "-x"}, dict(space, level="ceiling")], free="y") == \
        [240.0, None, 120.0]
    with pytest.raises(FeatureError, match="rises 24.00 in along x"):
        resolve(house, [{"el": "living.wall.north", "face": "-y"}, dict(space, level="ceiling")], free="x")
    assert resolve(house, {"el": "living.space.flat", "vertex": 2, "level": "ceiling"}).model == (100.0, 300.0, 96.0)
    assert resolve(house, {"el": "porch.slab.main", "vertex": 0, "level": "bottom"}).model == (80.0, -60.0, -18.0)
    assert resolve(house, {"el": "porch.slab.main", "corner": "+x-y", "level": "top"}).model == (170.0, -60.0, -14.0)
    with pytest.raises(FeatureError, match="x is pinned twice and the pins disagree"):
        resolve(house, {"el": "living.space.main", "vertex": 0, "x": "max"})


def test_roof_planes(house):
    roof = {"el": "roof.plane.south"}
    assert resolve(house, dict(roof, vertex=2)).model == (252.0, 90.0, 150.0)
    assert resolve(house, dict(roof, vertex=2, level="bottom")).model == (252.0, 90.0, 144.0)
    assert resolve(house, dict(roof, edge=[0, 1]), free="x").model == (None, -12.0, 100.0)
    with pytest.raises(FeatureError, match="not along a building axis"):
        resolve(house, dict(roof, edge=[0, 2]))
    # the top face over a plan point the rest of the reference pins
    over = [{"el": "living.wall.east", "face": "-x"}, {"el": "living.beam.main", "side": "-y"}, dict(roof, level="top")]
    assert m(house, over) == [240.0, 87.5, 100.0 + (87.5 + 12.0) * 50.0 / 102.0]


def test_skewed_walls_meet_as_planes(house):
    bay = {"el": "living.wall.bay"}
    # a = (0, 0), b = (40, 30): the left face 2 in out and end b meet at 50 u + 2 n
    corner = resolve(house, [dict(bay, face="left"), dict(bay, end="b")], free="z")
    assert list(corner.model[:2]) == pytest.approx([38.8, 31.6]) and corner.model[2] is None
    assert resolve(house, [dict(bay, face="+y"), dict(bay, end="b")], free="z").model[:2] == corner.model[:2]
    with pytest.raises(FeatureError, match="not a face"):
        resolve(house, [dict(bay, face="+x"), dict(bay, end="b")], free="z")
    # a face and a level leave a horizontal line along the wall, which runs nearest x
    top = resolve(house, dict(bay, face="left", level="top"), free="x")
    assert isinstance(top, Resolved) and [abs(v) for v in top.direction] == pytest.approx([0.8, 0.6, 0.0])
    assert top.model[2] == 96.0
    with pytest.raises(FeatureError, match='declare "free": "x"'):
        resolve(house, dict(bay, face="left", level="top"))


def test_combinators_free_axis_and_offsets_are_checked(house):
    north = {"el": "living.wall.north", "face": "-y"}
    with pytest.raises(FeatureError, match="pins only y"):
        resolve(house, north)
    with pytest.raises(FeatureError, match='declare "free": "z"'):
        resolve(house, [north, {"el": "living.wall.east", "face": "-x"}])
    with pytest.raises(FeatureError, match="free is 'z', but the reference pins z"):
        resolve(house, [north, {"el": "living.wall.east", "face": "-x"}, {"el": "living.space.flat", "level": "floor"}],
                free="z")
    with pytest.raises(FeatureError, match="pins disagree"):
        resolve(house, [north, {"el": "living.beam.main", "side": "-y"}], free="x")
    with pytest.raises(FeatureError, match="did you mean living.wall.north"):
        resolve(house, {"el": "living.wall.nort", "face": "-y"})
    with pytest.raises(FeatureError, match="selectors are"):
        resolve(house, {"el": "living.box.counter", "face": "-y"})
    with pytest.raises(FeatureError, match="pinning the same axes"):
        resolve(house, {"between": [{"el": "living.box.counter", "x": "min", "y": "min", "z": "max"},
                                    {"el": "living.box.counter", "x": "max", "y": "min"}]})
    crown = resolve(house, [north, {"el": "living.space.flat", "level": "ceiling"}], free="x", offset=[0, 0, -3.5])
    assert crown.model == (None, 180.0, 92.5)
    with pytest.raises(FeatureError, match="moves the free axis"):
        resolve(house, [north, {"el": "living.space.flat", "level": "ceiling"}], free="x", offset=[1, 0, 0])


def test_targets_name_surfaces_lines_and_points(house):
    face = resolve_target(house, {"el": "living.wall.north", "face": "-y"})
    assert isinstance(face, Surface) and (face.axis, face.value) == (1, 180.0)
    ceiling = resolve_target(house, {"el": "living.space.main", "level": "ceiling"})
    assert isinstance(ceiling, Surface) and ceiling.height.z_at(100.0, 50.0) == pytest.approx(106.0)
    corner = resolve_target(house, [{"el": "living.wall.north", "face": "-y"}, {"el": "living.wall.east", "face": "-x"}])
    assert isinstance(corner, Resolved) and corner.model == (240.0, 180.0, None)


# -------------------------------------------------------------------- picks file


PICKS = {"photo": "living-01.jpg", "notes": ["living room from the porch door"], "open_leaves": ["living.door.east"],
         "points": [
             {"label": "window head, left jamb", "px": [412.5, 233.0], "snap": True,
              "ref": {"el": "living.window.north", "jamb": "start", "level": "head", "face": "-y"}},
             {"label": "inside corner", "px": [1210.0, 300.0],
              "ref": [{"el": "living.wall.north", "face": "-y"}, {"el": "living.wall.east", "face": "-x"}], "free": "z"},
             {"label": "crown", "px": [760.0, 236.0], "role": "check", "sigma_px": 3,
              "ref": [{"el": "living.wall.north", "face": "-y"}, {"el": "living.space.flat", "level": "ceiling"}],
              "free": "x", "offset": [0, 0, -3.5], "offset_note": "crown moulding, not modelled"},
             {"label": "sofa arm", "px": [655.0, 590.0], "model": [180.0, 96.0, 25.0], "explicit_note": "furniture"}]}


def test_picks_resolve_against_the_house(house):
    ps = parse(json.loads(json.dumps(PICKS)), house)
    assert [c.label for c in ps.solve_points] == ["window head, left jamb", "inside corner", "sofa arm"]
    assert [c.label for c in ps.checks] == ["crown"]
    head, corner, crown, sofa = ps.points
    assert head.model == (60.0, 180.0, 84.0) and head.snap and head.sigma_px == 6.0
    assert corner.free_axis == 2 and corner.extra["model_src"] == {"living.wall.north": "cad", "living.wall.east": "cad"}
    assert crown.model == (None, 180.0, 92.5) and crown.sigma_px == 3.0 and crown.role == "check"
    assert sofa.extra["explicit_note"] == "furniture"
    assert ps.open_leaves == ["living.door.east"] and ps.notes == ["living room from the porch door"]
    model, err = reresolve({"label": "x", "ref": PICKS["points"][0]["ref"]}, house)
    assert model == (60.0, 180.0, 84.0) and err is None


@pytest.mark.parametrize("change,message", [
    (lambda d: d["points"][3].pop("explicit_note"), "'sofa arm' needs a non-empty explicit_note"),
    (lambda d: d["points"][2].pop("offset_note"), "'crown' offset and offset_note go together"),
    (lambda d: d["points"][0].update(colour="red"), "'window head, left jamb' has unknown fields"),
    (lambda d: d["points"][1].update(label="crown"), "duplicate label 'crown'"),
    (lambda d: d["points"][1].pop("free"), "'inside corner'.*declare \"free\": \"z\""),
    (lambda d: d.update(open_leaves=["living.window.north"]), "only a door's leaves open"),
    (lambda d: d["points"][0].update(role="maybe"), "role must be one of"),
])
def test_picks_problems_name_the_file_and_label(house, change, message):
    doc = json.loads(json.dumps(PICKS))
    change(doc)
    with pytest.raises(PicksError, match=message) as e:
        parse(doc, house, path="overlay/picks/living-01.json")
    assert "overlay/picks/living-01.json" in str(e.value)
