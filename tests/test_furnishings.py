"""Furnishings without SketchUp: the placement list's checks and the example list itself, the client refusing anything
but a furn/* piece before it sends anything, the one eval it sends under the look lock, and, where Ruby 3.2 or later
is on the PATH, bridge/furnishings.rb run under tests/ruby/furnishings_rb.rb: every recipe's parts closed solids
turned outward and filling exactly the size given, the front toward -y, and place, remove and list on a stand-in
SketchUp model."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from bridge import furnishings as furn
from rubyenv import RUBY, WHY, needs_ruby

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "ruby" / "furnishings_rb.rb"
FINISHES = {"wood_mid", "wood_dark", "wood_light", "oak", "carpet"} | set(furn.FABRICS)


def _piece(**kw):
    p = {"id": "chair", "recipe": "chair", "at": [1.0, 2.0, 0.0], "rotation": 90.0, "tag": "furnishings/dining",
         "finish": "oak", "params": {"seat_height": 17.0}}
    p.update(kw)
    return {k: v for k, v in p.items() if v is not None}


def _write(tmp_path, doc):
    path = tmp_path / "pieces.json"
    path.write_text(json.dumps(doc))
    return path


# ------------------------------------------------------------------ the list

def test_the_list_is_valid_against_the_palette():
    """furnishings/pieces.json, whatever it holds (an empty list too), checks clean against the palette."""
    if not furn.PALETTE.is_file():
        pytest.skip("no build/palette.json yet")
    assert all(furn.TAG_RE.fullmatch(p["tag"]) for p in furn.load_list())


def test_the_fabrics_join_the_palette():
    if not furn.PALETTE.is_file():
        pytest.skip("no build/palette.json yet")
    assert set(furn.FABRICS) <= set(furn.materials())


def test_the_listed_pieces_stand_inside_their_rooms():
    """Each recipe piece's plan footprint, from its size (the recipe's default when it gives none) and turn, lies inside
    a space of the zone its tag names, so no piece stands through a wall. A component keeps the size of its .skp file,
    which only SketchUp reads, so it is not checked here."""
    house = ROOT / "house.json"
    if not house.is_file():
        pytest.skip("no house.json yet")
    from shapely.geometry import Polygon
    doc = json.loads(house.read_text())
    spaces = {}
    for e in doc["elements"]:
        if e["type"] == "space":
            spaces.setdefault(e["zone"], []).append(Polygon(e["outline"]).buffer(0.01))
    for p in furn.load_list(finishes=FINISHES | set(json.loads(furn.PALETTE.read_text()))):
        if "recipe" not in p:
            continue
        room = p["tag"].split("/", 1)[1]
        assert room in spaces, (p["id"], sorted(spaces))
        w, d, _ = p.get("size") or furn.SIZES[p["recipe"]]
        c, s = np.cos(np.radians(p["rotation"])), np.sin(np.radians(p["rotation"]))
        corners = [(p["at"][0] + x * c - y * s, p["at"][1] + x * s + y * c)
                   for x, y in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))]
        assert any(sp.contains(Polygon(corners)) for sp in spaces[room]), p["id"]


@pytest.mark.parametrize("change, says", [
    ({"pieces": [_piece(), _piece()]}, "lists chair more than once"),
    ({"extra": 1}, "unknown keys extra"),
    ({"pieces": {"chair": {}}}, "must hold an object with a list of pieces"),
    ({"pieces": [_piece(id="HOUSE")]}, "'HOUSE' names no furnishings piece"),
    ({"pieces": [_piece(id="furn/chair")]}, "'furn/chair' names no furnishings piece"),
    ({"pieces": [_piece(id="dining chair")]}, "names no furnishings piece"),
    ({"pieces": [_piece(tag="kitchen/island")]}, "tag must be furnishings/<room>"),
    ({"pieces": [_piece(tag="furnishings/Dining")]}, "tag must be furnishings/<room>"),
    ({"pieces": [_piece(tag=None)]}, "tag must be furnishings/<room>"),
    ({"pieces": [_piece(at=[1.0, 2.0])]}, "at must be three numbers"),
    ({"pieces": [_piece(at=[1.0, 2.0, True])]}, "at must be three numbers"),
    ({"pieces": [_piece(rotation="north")]}, "rotation must be a number of degrees"),
    ({"pieces": [_piece(recipe=None)]}, "names a recipe or a component, one of the two"),
    ({"pieces": [_piece(component="furnishings/components/c.skp")]}, "names a recipe or a component, one of the two"),
    ({"pieces": [_piece(recipe="ottoman")]}, "names no recipe 'ottoman'"),
    ({"pieces": [_piece(size=[18.0, 20.0])]}, "size must be three positive numbers"),
    ({"pieces": [_piece(size=[18.0, -20.0, 30.0])]}, "size must be three positive numbers"),
    ({"pieces": [_piece(finish="teak")]}, "finish must be a palette key or a fabric"),
    ({"pieces": [_piece(params={"legs": 3})]}, "chair takes no param 'legs'"),
    ({"pieces": [_piece(params={"seat_height": -1})]}, "seat_height must be a positive number of inches"),
    ({"pieces": [_piece(params={"seat_finish": "teak"})]}, "seat_finish must be a palette key or a fabric"),
    ({"pieces": [_piece(recipe="sofa", params={"cushions": 0})]}, "cushions must be a whole number from 1 to 6"),
    ({"pieces": [_piece(recipe="sofa", params={"arm_left": 2})]}, "arm_left must be a whole number from 0 to 1"),
    ({"pieces": [_piece(recipe="bed", params={"pillows": 1.5})]}, "pillows must be a whole number from 0 to 6"),
    ({"pieces": [_piece(recipe=None, params=None, finish=None, component="../house.skp")]}, "lies outside"),
    ({"pieces": [_piece(recipe=None, params=None, finish=None, component="/abs/c.skp")]}, "relative to the project"),
    ({"pieces": [_piece(recipe=None, params=None, finish=None, component="furnishings/c.dae")]}, "a .skp file"),
    ({"pieces": [_piece(recipe=None, params=None, component="furnishings/components/c.skp")]},
     "a component, which keeps its own finish"),
    ({"pieces": [_piece(colour="red")]}, "unknown keys colour"),
])
def test_the_list_refuses_what_it_cannot_place(change, says, tmp_path):
    doc = {"pieces": [_piece()]}
    doc.update(change)
    with pytest.raises(furn.FurnishingsError) as e:
        furn.load_list(_write(tmp_path, doc), FINISHES, root=tmp_path)
    assert says in str(e.value)


def test_a_component_is_a_skp_file_under_furnishings(tmp_path):
    path = _write(tmp_path, {"pieces": [_piece(recipe=None, params=None, finish=None,
                                               component="furnishings/components/lamp.skp")]})
    [p] = furn.load_list(path, FINISHES, root=tmp_path)
    assert p == {"id": "chair", "at": [1.0, 2.0, 0.0], "rotation": 90.0, "tag": "furnishings/dining",
                 "component": str((tmp_path / "furnishings" / "components" / "lamp.skp").resolve())}
    with pytest.raises(furn.FurnishingsError, match="no component file"):
        furn.spec_for("place", [p])


def test_materials_are_the_palette_and_the_fabrics(tmp_path):
    palette = tmp_path / "palette.json"
    palette.write_text(json.dumps({"oak": {"rgb": [176, 134, 86], "note": "flooring"},
                                   "glass": {"rgb": [200, 220, 230], "alpha": 0.2}}))
    got = furn.materials(palette)
    assert got["oak"] == {"rgb": [176, 134, 86]} and got["glass"]["alpha"] == 0.2
    assert set(got) == {"oak", "glass"} | set(furn.FABRICS)


# ------------------------------------------------------------------ the client

class _Never:
    def eval(self, code):
        raise AssertionError("nothing may be sent")


@pytest.mark.parametrize("name", ["HOUSE", "FURNISHINGS", "zone/kitchen", "furn/", "furn/Dining", "furn/../x", "",
                                  "table 1", None, 3])
def test_the_client_refuses_anything_but_a_furn_piece_and_sends_nothing(name, tmp_path):
    lock = tmp_path / "lock" / "look.lock"
    with pytest.raises(furn.FurnishingsError, match="names no furnishings piece"):
        furn.piece_id(name)
    with pytest.raises(furn.FurnishingsError, match="names no furnishings piece"):
        furn.run("remove", ids=[name], bridge=_Never(), lock_path=lock)
    assert not lock.exists()                       # refused before the lock


def _list_and_palette(tmp_path):
    palette = tmp_path / "palette.json"
    palette.write_text(json.dumps({k: {"rgb": [1, 2, 3]} for k in FINISHES - set(furn.FABRICS)}))
    pieces = _write(tmp_path, {"pieces": [_piece(), _piece(id="sofa", recipe="sofa", params={"cushions": 2},
                                                           finish="fabric_gray", rotation=0.0)]})
    return pieces, palette


def test_the_client_sends_one_eval_under_the_look_lock(tmp_path):
    pieces, palette = _list_and_palette(tmp_path)
    lock = tmp_path / "out" / "look.lock"
    sent = []

    class Bridge:
        def eval(self, code):
            sent.append((code, lock.read_text()))
            spec = json.loads(code.split(".run(", 1)[1][1:-2].replace("\\'", "'").replace("\\\\", "\\"))
            if spec["cmd"] == "remove":
                return {"removed": spec["ids"][:1], "not_found": spec["ids"][1:]}
            return {"created": [p["id"] for p in spec["pieces"] if not spec["only"] or p["id"] in spec["only"]],
                    "updated": [], "unchanged": [], "moved": ["chair"], "tags_created": ["furnishings/dining"],
                    "materials_created": ["fabric_gray"], "spec": spec}

    lines = []
    assert furn.run("place", only=["furn/sofa"], who="probe", list_path=pieces, palette=palette, bridge=Bridge(),
                    echo=lines.append, lock_path=lock) == 0
    code, holder = sent[0]
    assert code.startswith(f"load '{furn.RB}'; HouseTwin::Furnishings.run('") and holder.startswith("probe (pid")
    assert "== created: sofa" in lines and "== tags created, shown: furnishings/dining" in lines
    assert any(ln.startswith("== moved by hand in SketchUp, left where they are") and ln.endswith("chair")
               for ln in lines), lines
    spec = json.loads(code.split(".run(", 1)[1][1:-2].replace("\\'", "'").replace("\\\\", "\\"))
    assert spec["only"] == ["sofa"] and [p["id"] for p in spec["pieces"]] == ["chair", "sofa"]
    assert spec["materials"]["fabric_gray"] == furn.FABRICS["fabric_gray"] and spec["materials"]["oak"]["rgb"]
    lines.clear()
    assert furn.run("remove", ids=["chair", "furn/nothing"], bridge=Bridge(), echo=lines.append, lock_path=lock) == 0
    assert lines == ["== removed: chair", "== not in FURNISHINGS: nothing"]
    assert lock.read_text() == ""                  # the holder's name cleared as the lock is let go
    with pytest.raises(furn.FurnishingsError, match="--only names pieces the list does not: ottoman"):
        furn.run("place", only=["ottoman"], list_path=pieces, palette=palette, bridge=_Never(), lock_path=lock)


def test_a_failure_in_sketchup_is_reported_with_exit_1(tmp_path):
    pieces, palette = _list_and_palette(tmp_path)

    class Refuses:
        def eval(self, code):
            raise RuntimeError("HouseTwin::Furnishings::Error: the sofa's arms 30 must stand between its seat\nbt")

    lines = []
    assert furn.run("place", list_path=pieces, palette=palette, bridge=Refuses(), echo=lines.append,
                    lock_path=tmp_path / "look.lock") == 1
    assert lines == ["== FAILED:", "   HouseTwin::Furnishings::Error: the sofa's arms 30 must stand between its seat"]


def test_the_ruby_string_survives_quotes_and_backslashes():
    text = "it's a \\path\\ 'quoted'"
    lit = furn.ruby_string(text)
    assert lit.startswith("'") and lit.endswith("'")
    if RUBY:
        out = subprocess.run([RUBY, "-e", f"print {lit}"], capture_output=True, text=True, check=True).stdout
        assert out == text


# ------------------------------------------------------------------ furnishings.rb under plain Ruby

def _solid(faces):
    """(closed and consistently turned, every face planar, the signed volume): faces wound counter-clockwise seen
    from outside share every edge with exactly one other face, which runs it the other way."""
    edges = {}
    planar = True
    vol = 0.0
    for f in faces:
        pts = np.array(f, dtype=float)
        n = np.zeros(3)
        for i in range(len(pts)):
            p, q = pts[i], pts[(i + 1) % len(pts)]
            n += np.cross(p, q)
            key = (tuple(np.round(p, 6)), tuple(np.round(q, 6)))
            edges[key] = edges.get(key, 0) + 1
        unit = n / np.linalg.norm(n)
        planar &= bool(np.all(np.abs((pts - pts[0]) @ unit) < 1e-6))
        for i in range(1, len(pts) - 1):
            vol += np.dot(pts[0], np.cross(pts[i], pts[i + 1])) / 6.0
    closed = all(c == 1 and edges.get((b, a)) == 1 for (a, b), c in edges.items())
    return closed, planar, vol


def _bbox(parts):
    pts = np.array([q for p in parts for f in p["faces"] for q in f])
    return list(pts.min(0)) + list(pts.max(0))


def _p(pid, tag="furnishings/living", recipe="box", at=(10.0, 20.0, 0.0), **kw):
    return {"id": pid, "recipe": recipe, "at": list(at), "rotation": 0.0, "tag": tag, **kw}


def _place(*pieces, only=()):
    return {"spec": {"cmd": "place", "only": list(only), "pieces": list(pieces),
                     "materials": {"wood_mid": {"rgb": [150, 104, 62]}, "wood_dark": {"rgb": [66, 48, 35]},
                                   "fabric_gray": {"rgb": [128, 128, 124]}}}}


RECIPE_CASES = [
    {"name": "box", "recipe": "box"},
    {"name": "table", "recipe": "table", "size": [72.0, 40.0, 29.0]},
    {"name": "chair", "recipe": "chair"},
    {"name": "sofa", "recipe": "sofa"},
    {"name": "sofa_module", "recipe": "sofa", "size": [40.0, 38.0, 30.0], "params": {"cushions": 1, "arm_right": 0}},
    {"name": "armless", "recipe": "sofa", "size": [30.0, 38.0, 30.0], "params": {"cushions": 1, "arm_left": 0,
                                                                                 "arm_right": 0}},
    {"name": "bed", "recipe": "bed"},
    {"name": "twin", "recipe": "bed", "size": [38.0, 75.0, 36.0], "params": {"pillows": 1}},
    {"name": "rug", "recipe": "rug"},
    {"name": "oak_chair", "recipe": "chair", "finish": "oak"},
    {"name": "linen_seat", "recipe": "chair", "finish": "oak", "params": {"seat_finish": "fabric_linen"}},
]
REFUSALS = [
    ({"recipe": "sofa", "params": {"arm_height": 16.0}}, "arms 16.0 must stand between its seat"),
    ({"recipe": "sofa", "size": [30.0, 36.0, 32.0], "params": {"cushions": 3}}, "3 cushions do not fit"),
    ({"recipe": "bed", "size": [60.0, 80.0, 20.0]}, "headboard 20.0 must stand over its mattress"),
    ({"recipe": "table", "size": [8.0, 8.0, 30.0]}, "legs (2.5 in, 2.0 in in) do not fit"),
    ({"recipe": "chair", "size": [18.0, 20.0, 24.0]}, "back 24.0 must stand over its seat"),
    ({"recipe": "chair", "params": {"legs": 3}}, "chair takes no param \"legs\""),
    ({"recipe": "sofa", "params": {"cushions": 9}}, "cushions must be a whole number in 1..6"),
    ({"recipe": "rug", "size": [10.0, 10.0]}, "size must be three positive numbers"),
    ({"recipe": "ottoman"}, "no recipe \"ottoman\""),
]


def _runs():
    """The stand-in runs: pieces placed, placed again, edited, moved by hand, removed and listed."""
    sofa = _p("sofa", recipe="sofa", finish="fabric_gray", at=(60.0, 100.0, 0.0))
    box = _p("cabinet", tag="furnishings/kitchen")
    return [
        {"name": "fresh", "tags": [["furnishings/living", False]], "materials": ["wood_mid", "wood_dark"],
         "steps": [_place(sofa, box)]},
        {"name": "again", "steps": [_place(sofa, box), _place(sofa, box)]},
        {"name": "edited", "steps": [_place(sofa, box), _place(dict(sofa, size=[90.0, 36.0, 32.0]),
                                                               dict(box, at=[30.0, 20.0, 0.0], rotation=90.0))]},
        {"name": "moved", "steps": [_place(sofa, box), {"move": "cabinet"},
                                    _place(sofa, dict(box, at=[30.0, 20.0, 0.0])), {"spec": {"cmd": "list"}}]},
        {"name": "only", "steps": [_place(sofa, box, only=["cabinet"])]},
        {"name": "remove", "steps": [_place(sofa, box), {"spec": {"cmd": "remove", "ids": ["sofa", "lamp"]}},
                                     {"spec": {"cmd": "list"}}]},
        {"name": "no_colour", "steps": [_place(dict(box, finish="walnut"))]},
        {"name": "list_empty", "steps": [{"spec": {"cmd": "list"}}]},
    ]


@pytest.fixture(scope="module")
def rb(tmp_path_factory):
    """tests/ruby/furnishings_rb.rb's answers for the recipe cases, refusals, reads, decisions and runs."""
    if RUBY is None:
        pytest.skip(WHY)
    cases = {
        "recipes": RECIPE_CASES + [dict(c, name=f"refused_{i}") for i, (c, _) in enumerate(REFUSALS)],
        "reads": [{"name": "ok", "spec": {"cmd": "place", "pieces": [_p("t", recipe="table")]}},
                  {"name": "house", "spec": {"cmd": "remove", "ids": ["HOUSE"]}},
                  {"name": "prefixed", "spec": {"cmd": "remove", "ids": ["furn/t"]}},
                  {"name": "twice", "spec": {"cmd": "place", "pieces": [_p("t"), _p("t")]}},
                  {"name": "relative", "spec": {"cmd": "place", "pieces": [
                      {"id": "c", "component": "furnishings/components/c.skp", "at": [0, 0, 0],
                       "tag": "furnishings/a"}]}},
                  {"name": "erase", "spec": {"cmd": "erase"}}],
        "decide": [{"name": "none", "count": 0, "recorded": None, "entry": "e", "current": None},
                   {"name": "copies", "count": 2, "recorded": {"entry": "e", "placed": "[1]"}, "entry": "e",
                    "current": [1.0]},
                   {"name": "foreign", "count": 1, "recorded": {"entry": None, "placed": None}, "entry": "e",
                    "current": [1.0]},
                   {"name": "moved", "count": 1, "recorded": {"entry": "e", "placed": "[1.0, 2.0]"}, "entry": "e",
                    "current": [1.0, 2.5]},
                   {"name": "still", "count": 1, "recorded": {"entry": "e", "placed": "[1.0, 2.0]"}, "entry": "e",
                    "current": [1.0, 2.0000001]},
                   {"name": "edited", "count": 1, "recorded": {"entry": "e", "placed": "[1.0, 2.0]"}, "entry": "f",
                    "current": [1.0, 2.0]}],
        "runs": _runs(),
    }
    path = tmp_path_factory.mktemp("furnishings_rb") / "cases.json"
    path.write_text(json.dumps(cases))
    res = subprocess.run([RUBY, str(RUNNER), str(furn.RB), str(path)], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-3000:]
    out = json.loads(res.stdout)
    crashed = {n: (r["crash"], r["backtrace"]) for n, r in out["runs"].items() if "crash" in r}
    assert not crashed, crashed
    return out


@needs_ruby
def test_the_python_list_knows_the_ruby_recipes_params(rb):
    assert set(rb["defaults"]) == set(furn.RECIPES)
    assert set(furn.SIZES) == set(furn.RECIPES)
    for name, params in furn.RECIPES.items():
        d = rb["defaults"][name]
        assert set(d["params"]) == params, name
        assert tuple(d["size"]) == furn.SIZES[name], name
        finishes = [d["finish"]] + [v for k, v in d["params"].items() if k.endswith("_finish") and v is not None]
        palette = set(json.loads(furn.PALETTE.read_text())) if furn.PALETTE.is_file() else {"wood_mid", "wood_dark"}
        assert all(f in palette | set(furn.FABRICS) for f in finishes), (name, finishes)
    assert {k: tuple(v) for k, v in rb["counts"].items()} == furn.COUNTS


@needs_ruby
@pytest.mark.parametrize("case", [c["name"] for c in RECIPE_CASES])
def test_every_part_is_a_closed_solid_turned_outward(rb, case):
    parts = rb["recipes"][case]["ok"]
    for p in parts:
        closed, planar, vol = _solid(p["faces"])
        assert closed and planar and vol > 0.0, (case, p["name"], closed, planar, vol)


@needs_ruby
@pytest.mark.parametrize("case", [c["name"] for c in RECIPE_CASES])
def test_a_recipe_fills_exactly_the_size_it_is_given(rb, case):
    """The list's size is the piece's plan and height: its origin the plan centre on the floor."""
    spec = next(c for c in RECIPE_CASES if c["name"] == case)
    w, d, h = spec.get("size") or rb["defaults"][spec["recipe"]]["size"]
    assert _bbox(rb["recipes"][case]["ok"]) == pytest.approx([-w / 2, -d / 2, 0.0, w / 2, d / 2, h], abs=1e-6)


@needs_ruby
def test_a_recipes_front_faces_minus_y(rb):
    """A sofa's back, a chair's back and a bed's headboard stand at +y, so the front faces -y at rotation 0."""
    named = {c: {p["name"]: _bbox([p]) for p in rb["recipes"][c]["ok"]} for c in ("sofa", "chair", "bed")}
    assert named["sofa"]["back"][1] > 0 and named["chair"]["back"][1] > 0 and named["bed"]["headboard"][1] > 0
    assert named["sofa"]["seat_cushion_1"][1] < named["sofa"]["back_cushion_1"][1]
    module = {p["name"] for p in rb["recipes"]["sofa_module"]["ok"]}
    assert "arm_left" in module and "arm_right" not in module
    assert {p["name"] for p in rb["recipes"]["bed"]["ok"]} >= {"frame", "headboard", "mattress", "pillow_2"}
    materials = {p["name"]: p["material"] for p in rb["recipes"]["sofa"]["ok"]}
    assert materials["foot_1"] == "wood_dark" and materials["back"] == "fabric_gray"
    # a chair's seat follows its finish unless it names its own
    assert {p["material"] for p in rb["recipes"]["oak_chair"]["ok"]} == {"oak"}
    linen = {p["name"]: p["material"] for p in rb["recipes"]["linen_seat"]["ok"]}
    assert linen.pop("seat") == "fabric_linen" and set(linen.values()) == {"oak"}


@needs_ruby
@pytest.mark.parametrize("i", range(len(REFUSALS)))
def test_a_recipe_refuses_what_it_cannot_draw(rb, i):
    assert REFUSALS[i][1] in rb["recipes"][f"refused_{i}"]["refused"]


@needs_ruby
def test_furnishings_rb_reads_only_furn_pieces_and_decides_what_place_does(rb):
    reads = rb["reads"]
    assert reads["ok"]["ok"]["pieces"][0]["size"] == rb["defaults"]["table"]["size"]
    assert "a piece id is lowercase letters" in reads["house"]["refused"]
    assert "a piece id is lowercase letters" in reads["prefixed"]["refused"]
    assert "repeated: t" in reads["twice"]["refused"]
    assert "component must be an absolute path" in reads["relative"]["refused"]
    assert "cmd must be one of place remove list" in reads["erase"]["refused"]
    assert rb["decide"] == {"none": "create", "copies": "copies", "foreign": "foreign", "moved": "moved",
                            "still": "unchanged", "edited": "update"}


def _pieces(run):
    [top] = [g for g in run["tree"] if g["name"] == "FURNISHINGS"]
    return {g["name"]: g for g in top["children"]}


@needs_ruby
def test_place_draws_each_piece_in_furnishings_on_its_tag(rb):
    run = rb["runs"]["fresh"]
    [step] = run["steps"]
    res = step["ok"]
    assert res["furnishings_created"] and sorted(res["created"]) == ["cabinet", "sofa"]
    held = _pieces(run)
    assert held["furn/sofa"]["at"] == [60.0, 100.0, 0.0] and held["furn/sofa"]["tag"] == "furnishings/living"
    assert held["furn/cabinet"]["tag"] == "furnishings/kitchen"
    # the owner's hidden tag stays hidden; a new tag comes up shown
    assert run["tags"]["furnishings/living"] is False and run["tags"]["furnishings/kitchen"] is True
    assert res["tags_created"] == ["furnishings/kitchen"]
    # the palette colour made the missing material; the model's own material is left alone
    assert res["materials_created"] == ["fabric_gray"] and run["materials"]["fabric_gray"] == [128, 128, 124]
    assert run["materials"]["wood_mid"] is None
    assert run["operations"] == ["start Furnishings: place", "commit"]


@needs_ruby
def test_place_again_changes_nothing_and_an_edit_brings_a_piece_to_the_list(rb):
    assert sorted(rb["runs"]["again"]["steps"][1]["ok"]["unchanged"]) == ["cabinet", "sofa"]
    edited = rb["runs"]["edited"]
    assert sorted(edited["steps"][1]["ok"]["updated"]) == ["cabinet", "sofa"]
    assert _pieces(edited)["furn/cabinet"]["at"] == [30.0, 20.0, 0.0]


@needs_ruby
def test_a_piece_moved_by_hand_stays_where_the_owner_put_it(rb):
    run = rb["runs"]["moved"]
    res = run["steps"][2]["ok"]
    assert res["moved"] == ["cabinet"] and res["unchanged"] == ["sofa"]
    assert _pieces(run)["furn/cabinet"]["at"] == [22.0, 20.0, 0.0]        # the owner's 12 in, not the list's 30
    listed = {r["id"]: r["moved"] for r in run["steps"][3]["ok"]["entities"]}
    assert listed == {"sofa": False, "cabinet": True}


@needs_ruby
def test_only_remove_list_and_a_refusal_before_anything_changes(rb):
    assert rb["runs"]["only"]["steps"][0]["ok"]["created"] == ["cabinet"]
    removed = rb["runs"]["remove"]
    assert removed["steps"][1]["ok"] == {"removed": ["sofa"], "not_found": ["lamp"]}
    assert [r["id"] for r in removed["steps"][2]["ok"]["entities"]] == ["cabinet"]
    assert removed["operations"] == ["start Furnishings: place", "commit", "start Furnishings: remove", "commit"]
    refused = rb["runs"]["no_colour"]
    assert "no colour for the materials walnut" in refused["steps"][0]["refused"]
    assert refused["operations"] == [] and refused["tree"] == []
    assert rb["runs"]["list_empty"]["steps"][0]["ok"] == {"furnishings": False, "entities": []}
