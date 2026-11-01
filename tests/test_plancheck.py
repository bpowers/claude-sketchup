"""tools/plancheck.py on a tiny house over a sheet drawn here: the cut (what is drawn, and how), wall faces measured
against the sheet's lines (exact where the model matches, the offset where a wall is moved, through a turned
registration too), the overlay image's pixels, and the plain plan."""
from __future__ import annotations

import json

import matplotlib
import numpy as np
import pytest
from PIL import Image

from extract import pdf as drawings
from tools import plancheck

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SCALE = 2.0 / 3.0                 # 1/4 in = 1 ft: inches of house per point
OUTER = [(0.0, 0.0), (240.0, 0.0), (240.0, 144.0), (0.0, 144.0)]
INNER = [(6.0, 6.0), (234.0, 6.0), (234.0, 138.0), (6.0, 138.0)]


def wall(name, a, b, openings=(), z0=0.0, top=96.0, thickness=6.0):
    return {"id": f"room.wall.{name}", "type": "wall", "zone": "room", "src": "cad", "a": list(a), "b": list(b),
            "thickness": thickness, "z0": z0, "top_a": top, "top_b": top, "kind": "exterior",
            "openings": list(openings)}


def house(west_x=3.0):
    """A 20 x 12 ft room of 6 in walls, its west wall's centreline at west_x (3 where it matches the sheet)."""
    door = {"id": "room.wall.south.door", "kind": "door", "at": 40.0, "width": 36.0, "sill_z": 0.0, "head_z": 80.0,
            "swing": {"hinge": "start", "side": "left"}}
    window = {"id": "room.wall.east.window", "kind": "window", "at": 48.0, "width": 48.0, "sill_z": 30.0,
              "head_z": 80.0}
    high = {"id": "room.wall.north.high", "kind": "window", "at": 100.0, "width": 30.0, "sill_z": 70.0, "head_z": 90.0}
    return {"ir_version": 1, "meta": {}, "zones": {"room": {"storey": 0}, "loft": {"storey": 1}}, "elements": [
        wall("south", (0, 3), (240, 3), [door]),
        wall("east", (237, 0), (237, 144), [window]),
        wall("north", (240, 141), (0, 141), [high]),
        wall("west", (west_x, 144), (west_x, 0)),
        {"id": "room.space.main", "type": "space", "zone": "room", "src": "cad", "outline": [list(p) for p in INNER],
         "floor_z": 0.0, "ceiling_z": 96.0},
        {"id": "loft.space.main", "type": "space", "zone": "loft", "src": "cad", "outline": [list(p) for p in INNER],
         "floor_z": 108.0},
        {"id": "room.box.counter", "type": "box", "zone": "room", "src": "cad", "bbox": [6, 114, 0, 96, 138, 36],
         "kind": "counter"},
        {"id": "room.box.upper", "type": "box", "zone": "room", "src": "cad", "bbox": [6, 126, 54, 96, 138, 84],
         "kind": "upper_cabinet"},
        {"id": "room.beam.main", "type": "beam", "zone": "room", "src": "cad", "a": [120, 6], "b": [120, 138],
         "width": 4.0, "depth": 8.0, "soffit_z": 88.0, "kind": "faux"},
        {"id": "room.column.post", "type": "column", "zone": "room", "src": "cad", "center": [120, 70],
         "shape": "square", "size": [6, 6], "z0": 0.0, "z1": 88.0},
    ]}


def sheet(path, reg_to_pt, rings):
    """A letter-landscape PDF with each ring (model points) drawn closed through reg_to_pt."""
    fig = plt.figure(figsize=(11, 8.5))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 792)
    ax.set_ylim(0, 612)
    ax.axis("off")
    for ring in rings:
        pts = [reg_to_pt(*p) for p in ring + ring[:1]]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color="k", lw=1)
    fig.savefig(path)
    plt.close(fig)
    return path


@pytest.fixture(params=[0.0, 90.0], ids=["square", "turned"])
def setup(request, tmp_path, monkeypatch):
    """(registrations path, house path): the room's sheet registered square, or turned a quarter."""
    monkeypatch.setattr(drawings, "OUT", tmp_path / "out" / "pdf")
    monkeypatch.setattr(plancheck, "OUT", tmp_path / "out" / "plancheck")
    rotation = request.param
    # the model's origin on the sheet: turned a quarter, the house's +x runs down the sheet and its +y to the right
    origin = drawings.solve_registration((150.0, 150.0) if rotation == 0 else (400.0, 520.0), (0.0, 0.0), SCALE,
                                         rotation)
    reg = drawings.Registration("room", tmp_path / "room.pdf", 0, origin, SCALE, rotation)
    sheet(reg.sheet, reg.to_pt, [OUTER, INNER])
    regs = tmp_path / "registrations.json"
    regs.write_text(json.dumps({"room": {"sheet": str(reg.sheet), "page": 0, "origin_pt": list(origin),
                                         "scale": SCALE, "rotation_deg": rotation}}))
    doc = tmp_path / "house.json"
    doc.write_text(json.dumps(house()))
    return regs, doc


def test_the_cut_draws_what_a_plan_shows():
    cut = plancheck.plan_cut(house(), 48.0)
    assert {w["id"] for w in cut["walls"]} == {"room.wall.south", "room.wall.east", "room.wall.north",
                                              "room.wall.west"}
    assert [s["id"] for s in cut["spaces"]] == ["room.space.main"]           # the loft's floor is over the cut
    assert [b["id"] for b in cut["floor"]] == ["room.box.counter"] and [b["id"] for b in cut["over"]] == \
        ["room.box.upper"]
    assert [b["id"] for b in cut["beams"]] == ["room.beam.main"] and len(cut["columns"]) == 1
    upstairs = plancheck.plan_cut(house(), 140.0)
    assert [s["id"] for s in upstairs["spaces"]] == ["loft.space.main"] and upstairs["walls"] == []
    # a door and a window the cut passes through are gaps in the wall; the high window is not
    solid = plancheck.walls_solid(cut["walls"], 48.0)
    whole = plancheck.walls_solid([dict(w, openings=[]) for w in cut["walls"]], 48.0)
    assert whole.area - solid.area == pytest.approx((36.0 + 48.0) * 6.0, rel=0.02)


def test_wall_faces_match_the_sheet_exactly_where_the_model_does(setup):
    regs, doc = setup
    lines = []
    _, rows = plancheck.over_sheet("room", house=doc, registrations=regs, layers=["None"], echo=lines.append)
    assert len(rows) == 8 and all(r["offset"] == pytest.approx(0.0, abs=0.05) for r in rows), rows
    assert lines[-1].startswith("8 of 8 wall faces matched a sheet line") and "0 over 1 in" in lines[-1]


def test_a_moved_wall_shows_its_offset_on_both_faces(setup):
    regs, doc = setup
    doc.write_text(json.dumps(house(west_x=5.0)))            # the west wall 2 in too far into the room
    lines = []
    _, rows = plancheck.over_sheet("room", house=doc, registrations=regs, layers=["None"], echo=lines.append)
    west = {r["face"]: r["offset"] for r in rows if r["id"] == "room.wall.west"}
    # a -> b runs -y, so its left is +x, the room: the sheet's room-side line stands 2 in back, the outer one 2 in out
    assert west == {"left": pytest.approx(-2.0, abs=0.05), "right": pytest.approx(2.0, abs=0.05)}
    assert "2 over 1 in" in lines[-1] and any(ln.startswith(">> room.wall.west") for ln in lines)


def test_the_overlay_paints_walls_where_the_sheet_draws_them(setup, tmp_path):
    regs, doc = setup
    lines = []
    out, rows = plancheck.over_sheet("room", house=doc, registrations=regs, dpi=144, out=tmp_path / "pc.png",
                                     echo=lines.append)
    # a sheet with no CAD layers is measured against all its lines without being asked
    assert len(rows) == 8 and out == tmp_path / "pc.png" and "the sheet has no CAD layers" in lines[0]
    img = np.asarray(Image.open(out).convert("RGB")).astype(int)
    # find the overlay's mapping again from the sheet: crop corner and header strip
    reg = drawings.registration("room", regs)
    geom = drawings.page(reg.sheet, 0)
    window = plancheck._grown(plancheck.extent(plancheck.plan_cut(house(), 48.0)))
    corners = [geom.to_px(*reg.to_pt(x, y), 144) for x in (window[0], window[2]) for y in (window[1], window[3])]
    left, top = int(min(c[0] for c in corners)), int(min(c[1] for c in corners))
    head = img.shape[0] - (int(np.ceil(max(c[1] for c in corners))) - top)

    def colour(x, y):
        px, py = geom.to_px(*reg.to_pt(x, y), 144)
        return img[int(py) - top + head, int(px) - left]

    r, g, b = colour(120.0, 141.0)                              # the north wall's middle
    assert r > 180 and g < 170 and b < 170
    assert tuple(colour(180.0, 100.0)) == pytest.approx((255, 255, 255), abs=3)    # the room's floor: paper
    r, g, b = colour(237.0, 72.0)                               # the east window's glass line
    assert b > 200 and r < 120


def test_layers_are_checked_against_the_sheet_and_a_layered_sheet_names_them(setup, monkeypatch):
    regs, doc = setup
    with pytest.raises(drawings.DrawingError, match="has no CAD layers, so there is no layer A-WALL"):
        plancheck.over_sheet("room", house=doc, registrations=regs, layers=["A-WALL"], echo=lambda s: None)
    monkeypatch.setattr(drawings, "layer_names", lambda pdf: ["A-WALL", "A-DOOR"])
    lines = []
    _, rows = plancheck.over_sheet("room", house=doc, registrations=regs, echo=lines.append)
    assert rows is None and lines == ["note: no wall faces measured; give --layers with the sheet's wall layers (it "
                                      "has A-WALL, A-DOOR)"]
    lines.clear()
    _, rows = plancheck.over_sheet("room", house=doc, registrations=regs, layers=["A-DOOR"], echo=lines.append)
    assert all(r["offset"] is None for r in rows) and lines[-1].startswith("note: are the lines on A-DOOR?")


def test_an_elevation_draws_the_facade_and_its_heights():
    cut = plancheck.elevation_cut(house(), "-y")
    # the south wall seen face on, the east and west walls edge on, the north wall too far behind to draw
    walls = sorted(tuple(map(tuple, ring)) for colour, ring, _ in cut["shapes"] if colour == "wall")
    assert walls == [((0.0, 0.0), (6.0, 0.0), (6.0, 96.0), (0.0, 96.0)),
                     ((0.0, 0.0), (240.0, 0.0), (240.0, 96.0), (0.0, 96.0)),
                     ((234.0, 0.0), (240.0, 0.0), (240.0, 96.0), (234.0, 96.0))]
    assert ("room.wall.south", "top", 0.0, 240.0, 96.0) in cut["edges"]
    assert ("room.wall.south.door", "head", 40.0, 76.0, 80.0) in cut["edges"]
    assert not [e for e in cut["edges"] if e[0] == "room.wall.south.door" and e[1] == "sill"]   # a door has none
    # seen from the north, u runs along -x: the north wall's high window, 100 in from its a end at x 240
    back = plancheck.elevation_cut(house(), "+y")
    assert ("room.wall.north.high", "sill", -140.0, -110.0, 70.0) in back["edges"]


def test_an_elevation_over_its_sheet_measures_each_height(tmp_path, monkeypatch):
    monkeypatch.setattr(drawings, "OUT", tmp_path / "out" / "pdf")
    monkeypatch.setattr(plancheck, "OUT", tmp_path / "out" / "plancheck")
    origin = drawings.solve_registration((150.0, 150.0), (0.0, 0.0), SCALE)
    reg = drawings.Registration("elev_south", tmp_path / "elev.pdf", 0, origin, SCALE, 0.0)
    sheet(reg.sheet, reg.to_pt, [[(0.0, 0.0), (240.0, 0.0), (240.0, 96.0), (0.0, 96.0)],
                                 [(40.0, 0.0), (76.0, 0.0), (76.0, 80.0), (40.0, 80.0)]])
    regs = tmp_path / "registrations.json"
    regs.write_text(json.dumps({"elev_south": {"sheet": str(reg.sheet), "page": 0, "origin_pt": list(origin),
                                               "scale": SCALE, "rotation_deg": 0.0}}))
    doc = tmp_path / "house.json"
    taller = house()
    taller["elements"][0]["openings"][0]["head_z"] = 82.0          # the door's head 2 in above the sheet's
    doc.write_text(json.dumps(taller))
    lines = []
    out, rows = plancheck.over_sheet("elev_south", house=doc, facing="-y", registrations=regs, echo=lines.append)
    got = {(r["id"], r["face"]): r["offset"] for r in rows}
    assert got[("room.wall.south", "top")] == pytest.approx(0.0, abs=0.05)
    assert got[("room.wall.south.door", "head")] == pytest.approx(-2.0, abs=0.05)
    assert out == tmp_path / "out" / "plancheck" / "elev_south.png" and "heights matched a sheet line" in lines[-1]


def test_the_plain_plan_is_to_scale(tmp_path):
    doc = tmp_path / "house.json"
    doc.write_text(json.dumps(house()))
    out = plancheck.plain(house=doc, px_per_ft=24.0, window=(-24.0, -24.0, 264.0, 168.0), out=tmp_path / "plan.png")
    img = np.asarray(Image.open(out).convert("RGB")).astype(int)
    width, height = 288 * 2, 192 * 2                            # 24 px a foot: 2 px an inch
    assert img.shape[1] == width
    head = img.shape[0] - height
    r, g, b = img[head + (168 - 141) * 2, (120 + 24) * 2]       # the north wall's middle
    assert r > 180 and g < 170 and b < 170


def test_the_command_line_writes_and_refuses_plainly(tmp_path, capsys):
    doc = tmp_path / "house.json"
    doc.write_text(json.dumps(house()))
    assert plancheck.main(["--plain", "--house", str(doc), "-o", str(tmp_path / "p.png")]) == 0
    assert (tmp_path / "p.png").is_file()
    assert plancheck.main(["--plain", "--house", str(tmp_path / "none.json")]) == 2
    assert "compile the house first" in capsys.readouterr().err
    assert plancheck.main(["--plain", "--house", str(doc), "--z", "500"]) == 2
    assert "draws nothing" in capsys.readouterr().err
    assert plancheck.main(["--plain", "--house", str(doc), "--elevation", "-y", "-o", str(tmp_path / "e.png")]) == 0
    assert (tmp_path / "e.png").is_file()
