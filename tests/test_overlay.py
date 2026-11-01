"""Render requests, the solve command on a synthetic photo, compose, render (through a stand-in bridge), restamp,
measure, annotate and the gallery. Everything runs in a temporary folder; nothing talks to SketchUp."""
import json
import math
import os

import cv2
import numpy as np
import pytest

from overlay import annotate, compose, gallery, measure, paths, render, restamp, solve
from overlay.camera import Camera
from overlay.features import House
from overlay.request import (RequestError, crop_render, default_hide_tags, load_request, make_request, photo_camera,
                             request_camera)

W, H = 960, 720
F = 480.0 / math.tan(math.radians(37.5))
TRUTH = Camera.from_euler((W, H), F, (120.0, 10.0, 58.0), 90.0, 0.0, 0.0, (0.0, -36.0))

HOUSE = {
    "ir_version": 1, "meta": {"project": {"name": "test room"}},
    "zones": {"living": {"storey": 0}, "upper": {"storey": 1}, "roof": {"storey": 9},
              "porch": {"storey": 0, "outdoor": True}},
    "elements": [
        {"id": "living.wall.north", "type": "wall", "zone": "living", "src": "cad", "a": [-3, 183], "b": [243, 183],
         "thickness": 6, "z0": 0, "top_a": 96, "top_b": 96, "kind": "exterior",
         "openings": [{"id": "living.window.north", "kind": "window", "at": 63, "width": 48, "sill_z": 36,
                       "head_z": 84, "unit": {"frame": 2, "verticals": [24], "mullion": 2, "glazed": True}}]},
        {"id": "living.wall.east", "type": "wall", "zone": "living", "src": "cad", "a": [243, -60], "b": [243, 183],
         "thickness": 6, "z0": 0, "top_a": 96, "top_b": 96, "kind": "exterior", "openings": []},
        {"id": "living.wall.west", "type": "wall", "zone": "living", "src": "cad", "a": [-3, -60], "b": [-3, 183],
         "thickness": 6, "z0": 0, "top_a": 96, "top_b": 96, "kind": "exterior", "openings": []},
        {"id": "living.box.counter", "type": "box", "zone": "living", "src": "measured",
         "bbox": [150, 155, 0, 240, 180, 36], "kind": "counter"},
        {"id": "living.space.main", "type": "space", "zone": "living", "src": "cad",
         "outline": [[0, -60], [240, -60], [240, 180], [0, 180]], "floor_z": 0, "ceiling_z": 96},
        {"id": "porch.space.deck", "type": "space", "zone": "porch", "src": "cad",
         "outline": [[0, -160], [240, -160], [240, -66], [0, -66]], "floor_z": -14},
    ]}

N, E, WW = ({"el": f"living.wall.{k}", "face": f} for k, f in (("north", "-y"), ("east", "-x"), ("west", "+x")))
CEIL, FLOOR = {"el": "living.space.main", "level": "ceiling"}, {"el": "living.space.main", "level": "floor"}
WIN, BOX = {"el": "living.window.north", "face": "-y"}, {"el": "living.box.counter"}
# (label, reference, free axis, a model point on it where the pick is read, role)
PICKS = [
    ("far left corner, ceiling", [N, WW, CEIL], None, (0, 180, 96), "solve"),
    ("far left corner, floor", [N, WW, FLOOR], None, (0, 180, 0), "solve"),
    ("far right corner, ceiling", [N, E, CEIL], None, (240, 180, 96), "solve"),
    ("far right corner, floor", [N, E, FLOOR], None, (240, 180, 0), "solve"),
    ("window, left jamb at sill", dict(WIN, jamb="start", level="sill"), None, (60, 180, 36), "solve"),
    ("window, right jamb at sill", dict(WIN, jamb="end", level="sill"), None, (108, 180, 36), "solve"),
    ("window, left jamb at head", dict(WIN, jamb="start", level="head"), None, (60, 180, 84), "solve"),
    ("window, right jamb at head", dict(WIN, jamb="end", level="head"), None, (108, 180, 84), "solve"),
    ("counter, front left top", dict(BOX, x="min", y="min", z="max"), None, (150, 155, 36), "solve"),
    ("counter, front left foot", dict(BOX, x="min", y="min", z="min"), None, (150, 155, 0), "solve"),
    ("west wall at the floor", [WW, FLOOR], "y", (0, 170, 0), "solve"),
    ("east wall at the ceiling", [E, CEIL], "y", (240, 175, 96), "solve"),
    ("north wall at the ceiling", [N, CEIL], "x", (180, 180, 96), "solve"),
    ("counter, back left top", dict(BOX, x="min", y="max", z="max"), None, (150, 180, 36), "check"),
    ("west wall at the ceiling", [WW, CEIL], "y", (0, 172, 96), "check"),
]


def wireframe(house: dict) -> list:
    """3D segments of the room as a render would draw them: the far wall, the side walls' floor and ceiling lines,
    the window and the counter."""
    box = next(e for e in house["elements"] if e["id"] == "living.box.counter")["bbox"]
    segs = [((0, 180, 0), (240, 180, 0)), ((0, 180, 96), (240, 180, 96)), ((0, 180, 0), (0, 180, 96)),
            ((240, 180, 0), (240, 180, 96))]
    segs += [((x, 60, z), (x, 180, z)) for x in (0, 240) for z in (0, 96)]
    segs += [((60, 180, 36), (108, 180, 36)), ((60, 180, 84), (108, 180, 84)), ((60, 180, 36), (60, 180, 84)),
             ((108, 180, 36), (108, 180, 84))]
    c = [(x, y, z) for x in (box[0], box[3]) for y in (box[1], box[4]) for z in (box[2], box[5])]
    segs += [(a, b) for i, a in enumerate(c) for b in c[i + 1:] if sum(p != q for p, q in zip(a, b)) == 1]
    return segs


def rasterize(cam: Camera, segs: list, *, fg: int, bg: int, width: int = 2) -> np.ndarray:
    img = np.full((cam.height, cam.width, 3), bg, dtype=np.uint8)
    for a, b in segs:
        pts = np.linspace(np.asarray(a, float), np.asarray(b, float), 60)
        uv, z = cam.project(pts, return_depth=True)
        uv = uv[z > 1.0]
        if len(uv) >= 2:
            p = np.round((uv - 0.5) * 16).astype(np.int64).clip(-2 ** 20, 2 ** 20).astype(np.int32)
            cv2.polylines(img, [p.reshape(-1, 1, 2)], False, (fg, fg, fg), width, cv2.LINE_AA, shift=4)
    return img


def fake_render(req_path, house: dict) -> dict:
    """A stand-in for SketchUp: black edges on white through the request's centred camera, written at its out."""
    req = load_request(req_path)
    img = rasterize(request_camera(req), wireframe(house), fg=0, bg=255)
    cv2.imwrite(req["out"], img)
    return {"out": req["out"], "width": req["width"], "height": req["height"]}


class FakeBridge:
    def __init__(self, house_path):
        self.house_path = house_path
        self.calls = 0

    def render(self, req_path, expect_sha256=None):
        self.calls += 1
        return dict(fake_render(req_path, json.loads(open(self.house_path).read())), ir_sha256=expect_sha256)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temporary repository: house.json, a synthetic photo of the room and its picks."""
    for name, sub in (("PHOTOS", "sources/photos"), ("PICKS", "overlay/picks"), ("CAMERAS", "overlay/cameras"),
                      ("REQUESTS", "overlay/requests"), ("OUT", "out"), ("RENDERS", "out/renders"),
                      ("OVERLAYS", "out/overlays"), ("ANNOTATE", "out/annotate")):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(paths, name, tmp_path / sub)
    house = tmp_path / "house.json"
    house.write_text(json.dumps(HOUSE, indent=1))
    monkeypatch.setattr(paths, "HOUSE", house)
    photo = rasterize(TRUTH, wireframe(HOUSE), fg=235, bg=90)
    cv2.imwrite(str(tmp_path / "sources/photos/living-01.png"), photo)
    rng = np.random.default_rng(0)
    points = []
    for label, ref, free, X, role in PICKS:
        px = TRUTH.project(np.array(X, float)) + rng.normal(0, 0.3, 2)
        p = {"label": label, "px": [round(float(v), 2) for v in px], "ref": ref, "sigma_px": 1.0}
        if free:
            p["free"] = free
        if role != "solve":
            p["role"] = role
        points.append(p)
    (tmp_path / "overlay/picks/living-01.json").write_text(json.dumps(
        {"photo": "living-01.png", "notes": ["Living room toward the window"], "points": points}, indent=1))
    return tmp_path


# --------------------------------------------------------------------- requests


def blob_canvas(size, uv, sigma=1.2):
    """Gaussian dots at continuous positions, each pixel sampled at its centre."""
    w, h = size
    img = np.zeros((h, w))
    r = int(np.ceil(4 * sigma))
    for u, v in uv:
        jj, ii = np.mgrid[max(int(v) - r, 0):min(int(v) + r + 1, h), max(int(u) - r, 0):min(int(u) + r + 1, w)]
        img[jj, ii] += np.exp(-(((ii + 0.5) - u) ** 2 + ((jj + 0.5) - v) ** 2) / (2 * sigma ** 2))
    return img


def centroid(img, uv, r=5):
    i0, j0 = int(np.floor(uv[0])) - r, int(np.floor(uv[1])) - r
    patch = img[j0:j0 + 2 * r + 1, i0:i0 + 2 * r + 1]
    jj, ii = np.mgrid[j0:j0 + 2 * r + 1, i0:i0 + 2 * r + 1]
    return np.array([(patch * (ii + 0.5)).sum() / patch.sum(), (patch * (jj + 0.5)).sum() / patch.sum()])


def scene_points(n=40, seed=0):
    return np.random.default_rng(seed).uniform([20.0, 120.0, 5.0], [220.0, 180.0, 90.0], (n, 3))


@pytest.mark.parametrize("offset", [(0.0, 0.0), (37.0, -61.0), (-12.0, 20.0)])
def test_canvas_and_crop_round_trip(offset):
    cam = TRUTH.replace(principal_offset=offset)
    req = json.loads(json.dumps(make_request(cam, "/abs/r.png", hide_tags=[])))
    x0, y0, cw, ch = req["crop"]
    assert (cw, ch) == (W, H) and all(isinstance(v, int) for v in req["crop"])
    assert req["width"] == W + 2 * abs(int(offset[0])) and req["height"] == H + 2 * abs(int(offset[1]))
    X = scene_points()
    rc = request_camera(req)
    assert rc.principal_offset == (0.0, 0.0) and rc.f == pytest.approx(cam.f)
    np.testing.assert_allclose(rc.project(X) - [x0, y0], cam.project(X), atol=1e-6)
    np.testing.assert_allclose(photo_camera(req).project(X), cam.project(X), atol=1e-6)


@pytest.mark.parametrize("aspect,lens", [(1.06, (0.0, 0.0)), (1.0, (-0.06, 0.01)), (0.97, (0.04, 0.0))])
def test_a_rendered_canvas_is_cut_back_onto_the_photo_pixels(aspect, lens):
    """Stand-in for SketchUp: dots drawn by the request's centred square-pixel pinhole camera land, after
    crop_render, where the photo camera (pixel aspect and lens included) puts them. (dx is rounded to whole pixels
    in a request, so it is whole here; a fractional dy is kept exact when the render is resampled anyway.)"""
    cam = TRUTH.replace(principal_offset=(17.0, -41.6), pixel_aspect=aspect, distortion=lens)
    req = json.loads(json.dumps(make_request(cam, "/abs/r.png", hide_tags=[])))
    X = scene_points(30, seed=1)
    photo = crop_render(blob_canvas((req["width"], req["height"]), request_camera(req).project(X)), req)
    assert photo.shape == (H, W)
    uvs = [uv for uv in cam.project(X) if 8 < uv[0] < W - 8 and 8 < uv[1] < H - 8]
    assert len(uvs) > 15
    errs = [np.linalg.norm(centroid(photo, uv) - uv) for uv in uvs]
    assert max(errs) < 0.1, errs
    with pytest.raises(RequestError, match="render is"):
        crop_render(np.zeros((10, 10)), req)


def test_default_hide_tags_follow_the_eye_and_the_storeys():
    tags, why = default_hide_tags(HOUSE, (120.0, 10.0, 58.0))
    assert tags == ["zone/upper", "zone/roof"] and "living.space.main" in why
    assert default_hide_tags(HOUSE, (120.0, -100.0, 50.0))[0] == []           # on the outdoor deck
    assert default_hide_tags(HOUSE, (120.0, -400.0, 60.0))[0] == []           # in no space
    assert default_hide_tags(HOUSE, (120.0, 10.0, 130.0))[0] == []            # above the ceiling: in no space


# ------------------------------------------------------------- the whole loop


def test_solve_compose_render_restamp(project, capsys, monkeypatch):
    house = paths.HOUSE
    assert solve.main(["living-01", "--house", str(house)]) == 0
    doc = json.loads((paths.CAMERAS / "living-01.json").read_text())
    cam = Camera.from_json(doc)
    opts = doc["solve"]["options"]
    assert opts["attitude"] == "level"                       # parallel verticals: auto chose a level camera
    assert opts["principal_offset"][1] == pytest.approx(-36.0, abs=3.0)   # the horizon started dy
    assert np.linalg.norm(cam.eye - TRUTH.eye) < 1.0
    assert cam.principal_offset[1] == pytest.approx(-36.0, abs=1.0)
    assert doc["residual_px"]["rms"] < 1.0 and doc["check_residual_px"]["n"] == 2
    assert doc["check_residual_px"]["max"] < 2.0
    assert doc["house"]["sha256"] == House.load(house).sha256
    assert doc["eye_space"] == {"id": "living.space.main", "zone": "living"}
    assert doc["solve"]["guard"]["refused"] == []
    req = json.loads((paths.REQUESTS / "living-01.json").read_text())
    assert req["hide_tags"] == ["zone/upper", "zone/roof"] and req["expect_ir_sha256"] == doc["house"]["sha256"]
    assert (paths.OVERLAYS / "living-01_overlay.png").exists()

    # a render through the request lands on the photo's edges
    bridge = FakeBridge(house)
    render.render_photo("living-01", house_path=house, bridge=bridge)
    stats = compose.compose_photo("living-01", house_path=house)
    assert stats["chamfer"]["render_to_photo_px"] < 1.5 and stats["warnings"] == []
    assert compose.compose_photo("living-01", clean=True, house_path=house)["clean"]
    assert cv2.imread(str(paths.OVERLAYS / "living-01_clean.png")).shape == (H, W, 3)

    # a render through a camera 6 in off misses
    wrong = fake_render(paths.REQUESTS / "living-01.json", HOUSE)
    req_w = make_request(cam.replace(eye=cam.eye + [6.0, 0.0, 0.0]), wrong["out"], hide_tags=[])
    cv2.imwrite(wrong["out"], rasterize(request_camera(req_w), wireframe(HOUSE), fg=0, bg=255))
    assert compose.compose_photo("living-01", house_path=house)["chamfer"]["render_to_photo_px"] > 3.0

    # a render older than its request is stale: reported, not scored, and no clean overlay is made from it
    t = os.path.getmtime(wrong["out"])
    os.utime(paths.REQUESTS / "living-01.json", (t + 10, t + 10))
    stale = compose.compose_photo("living-01", house_path=house)
    assert stale.get("stale") and "chamfer" not in stale
    with pytest.raises(compose.ComposeError, match="no usable render"):
        compose.compose_photo("living-01", clean=True, house_path=house)

    # a trial re-stamp on the same house reproduces the camera and writes nothing
    before = (paths.CAMERAS / "living-01.json").read_text()
    assert restamp.main(["--trial", "living-01", "--house", str(house)]) == 0
    assert "living-01: trial" in capsys.readouterr().out
    assert (paths.CAMERAS / "living-01.json").read_text() == before
    info = restamp.restamp("living-01", house=House.load(house), trial=True)
    assert info["eye_moved_in"] < 1e-3
    # picks edited since the solve: the re-solve starts afresh from many starts, and lands in the same place
    t = os.path.getmtime(paths.CAMERAS / "living-01.json")
    os.utime(paths.PICKS / "living-01.json", (t + 5, t + 5))
    assert restamp.restamp("living-01", house=House.load(house), trial=True)["eye_moved_in"] < 1e-3
    os.utime(paths.PICKS / "living-01.json", (t - 5, t - 5))

    # the model moves: the render refuses the old camera, and a re-stamp solves, renders and composes again
    moved = json.loads(json.dumps(HOUSE))
    next(e for e in moved["elements"] if e["id"] == "living.box.counter")["bbox"][1] = 153
    house.write_text(json.dumps(moved, indent=1))
    with pytest.raises(render.RenderRefused, match="moved since the solve"):
        render.render_photo("living-01", house_path=house, bridge=bridge)
    assert "moved 2.00 in" in " ".join(compose.compose_photo("living-01", house_path=house)["warnings"])
    info = restamp.restamp("living-01", house=House.load(house), trial=False, bridge=bridge)
    assert info["status"] == "ok", info
    assert json.loads((paths.CAMERAS / "living-01.json").read_text())["house"]["sha256"] == House.load(house).sha256
    assert restamp.restamp("living-01", house=House.load(house), trial=False, bridge=bridge)["status"] == "up to date"
    # an edit to build.rb draws differently, so a render older than it is out of date
    builder = project / "build.rb"
    builder.write_text("# edited")
    later = (paths.RENDERS / "living-01_edges.png").stat().st_mtime + 5
    os.utime(builder, (later, later))
    monkeypatch.setattr(paths, "BUILDERS", (builder,))
    assert not restamp.up_to_date("living-01", House.load(house).sha256)


def test_segments_join_the_solve(project):
    sol = solve.solve_photo("living-01", solve.Options(use_lines=True), house=House.load(paths.HOUSE))
    used = sol.doc["solve"]["segments_used"]
    assert used.get("z", 0) >= 4 and used.get("y", 0) >= 2, used
    assert np.linalg.norm(np.subtract(sol.doc["eye"], TRUTH.eye)) < 1.0
    assert sol.doc["solve"]["options"]["use_lines"] is True


def test_the_guard_keeps_a_loose_camera_out(project, capsys):
    # the far wall's picks alone, focal length and principal point free: refused, nothing written
    pf = paths.PICKS / "living-01.json"
    doc = json.loads(pf.read_text())
    doc["points"] = [p for p in doc["points"] if p["label"].startswith(("far", "window"))]
    pf.write_text(json.dumps(doc))
    assert solve.main(["living-01", "--house", str(paths.HOUSE), "--attitude", "free"]) == 3
    assert "REFUSED" in capsys.readouterr().err
    assert not (paths.CAMERAS / "living-01.json").exists()


def test_bad_picks_are_reported_not_solved(project, capsys):
    pf = paths.PICKS / "living-01.json"
    doc = json.loads(pf.read_text())
    doc["points"][0]["ref"][0]["el"] = "living.wall.nort"
    pf.write_text(json.dumps(doc))
    assert solve.main(["living-01", "--house", str(paths.HOUSE)]) == 2
    err = capsys.readouterr().err
    assert "living-01.json" in err and "far left corner, ceiling" in err and "did you mean living.wall.north" in err


# ---------------------------------------------------------------------- measure


def test_back_projection_onto_planes_heights_and_lines():
    cam = TRUTH.replace(principal_offset=(9.0, -40.0), pixel_aspect=1.03, distortion=(-0.05, 0.01))
    house = House(json.loads(json.dumps(HOUSE)))
    X = np.array([100.0, 180.0, 50.0])
    uv = cam.project(X)
    pc = measure.PhotoCamera("t", None, {"check_residual_px": {"rms": 1.0}}, cam)
    for target in ("y=180", '{"el": "living.wall.north", "face": "-y"}'):
        r = measure.bp_point(pc, uv, measure.parse_target(target, lambda: house))
        assert r["model"] == pytest.approx(list(X), abs=1e-6)
        assert r["pinned_axis"] == 1 and r["at_checks_rms_in"] is not None
    # per px, a point at depth d moves about d / f inches
    r = measure.bp_point(pc, uv, measure.parse_target("y=180", lambda: house))
    assert abs(r["per_px_u"][0]) == pytest.approx(170.0 / cam.f, rel=0.1)
    ceil = cam.project(np.array([60.0, 150.0, 96.0]))
    r = measure.bp_point(pc, ceil, measure.parse_target('{"el": "living.space.main", "level": "ceiling"}', lambda: house))
    assert r["model"] == pytest.approx([60.0, 150.0, 96.0], abs=1e-6)
    # an inside corner is a line: the reading is its point nearest the ray, and the ray's miss
    corner = measure.parse_target('[{"el": "living.wall.north", "face": "-y"}, {"el": "living.wall.east", "face": "-x"}]',
                                  lambda: house)
    r = measure.bp_point(pc, cam.project(np.array([240.0, 180.0, 70.0])), corner)
    assert r["model"] == pytest.approx([240.0, 180.0, 70.0], abs=1e-5) and r["miss_px"] < 1e-4
    # a line pick on a plane: its run and length
    ends = cam.project(np.array([[60.0, 180.0, 84.0], [108.0, 180.0, 84.0]]))
    r = measure.bp_line(pc, ends, measure.parse_target("y=180", lambda: house))
    assert r["runs_along"] == "x" and r["length_in"] == pytest.approx(48.0, abs=1e-5)
    with pytest.raises(measure.MeasureError, match="names the point"):
        measure.parse_target('{"el": "living.box.counter", "x": "min", "y": "min", "z": "max"}', lambda: house)


def test_triangulation_from_two_photos():
    a = TRUTH
    b = Camera.look_at((W, H), (220.0, 40.0, 62.0), (100.0, 170.0, 40.0), fov_v_deg=60.0, pixel_aspect=1.02,
                       distortion=(-0.04, 0.0))
    X = np.array([150.0, 155.0, 36.0])
    items = [(measure.PhotoCamera(n, None, {}, c), c.project(X)) for n, c in (("a", a), ("b", b))]
    r = measure.tri_points(items)
    assert r["model"] == pytest.approx(list(X), abs=1e-6) and r["widest_angle_deg"] > 20
    r = measure.tri_points([(pc, uv + [2.0, 0.0]) for pc, uv in items])
    assert all(p["miss_px"] > 0.1 for p in r["photos"])


# --------------------------------------------------------- annotate, gallery


def test_annotate_writes_an_overview_tiles_and_crosshair_crops(project):
    index = annotate.annotate("living-01")
    names = [f["file"] for f in index["files"]]
    assert "overview.png" in names and any(n.startswith("tile_") for n in names)
    crops = annotate.annotate(paths.PHOTOS / "living-01.png", at=[(412.5, 233.0)])
    assert [f["file"] for f in crops["files"]] == ["at_u0412_v0233.png"]
    assert (paths.ANNOTATE / "living-01" / "at_u0412_v0233.png").exists()


def test_gallery_is_one_page_with_every_photo(project):
    assert solve.main(["living-01", "--house", str(paths.HOUSE)]) == 0
    render.render_photo("living-01", house_path=paths.HOUSE, bridge=FakeBridge(paths.HOUSE))
    compose.compose_photo("living-01", clean=True, house_path=paths.HOUSE)
    res = gallery.build(paths.OUT / "gallery.html", title="Test room", notes="# Changes\n- moved the **counter**")
    page = res["page"].read_text()
    assert res["cards"] == 1 and "Living room toward the window" in page and "<b>counter</b>" in page
    assert page.count("data:image/jpeg;base64,") == 2 and 'type="range"' in page
