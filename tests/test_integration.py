"""The parts held together, without SketchUp: the house's sources through housec and areasc into build.rb's sync (under
plain Ruby against tests/ruby/sketchup_stub.rb, driven by tests/ruby/sync_cases.rb), a photo's render request through
bridge/client.py into render.rb, and the photo tools' reading of house.json against the planner's.

- A first sync holds exactly what the manifest places, each piece drawn once and instanced under its other chains.
- A second sync of the same manifest builds, instances and erases nothing and redraws no crease.
- A change to one number of one design alternative rebuilds exactly the pieces that number reaches, erases their old
  groups and redraws only the creases naming them; switching back puts the first HOUSE back. (The example cottage's
  own numbers; skipped once house.star is another house.)
- No sync changes what the owner set (look.py's owner-state probe, read before and after each one).
- A render request overlay.request makes for a camera in a room, sent the way bridge/client.py sends it, reads in
  render.rb: every tag it hides is one the sync made, and the house sha it expects is the one HOUSE records.
- Every element of house.json can be named by a pick's feature reference, and the photo tools see each window's and
  door's lights where build.rb draws them.

    uv run pytest tests/test_integration.py        # the Ruby half skips without Ruby 3.2 or later (tests/rubyenv.py)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from shapely.geometry import Polygon

import areasc
import housec
import look
from bridge.client import Bridge
from ir.plan import plan_document
from overlay import features
from overlay.camera import Camera
from overlay.request import default_hide_tags, make_request
from rubyenv import RUBY, needs_ruby

ROOT = Path(__file__).resolve().parent.parent
HOUSE = ROOT / "house.json"
CASES = ROOT / "tests" / "ruby" / "sync_cases.rb"
# One number of one alternative, as zones/areas/kitchen.star writes it, and what it becomes.
ISLAND_LENGTH = ("ISLAND_LENGTH = 84.0", "ISLAND_LENGTH = 90.0")
ISLAND_PIECES = {"kitchen.island.base", "kitchen.island.counter"}


def _is_cottage():
    try:
        return json.loads(HOUSE.read_text())["meta"]["project"]["name"] == "Example cottage"
    except (OSError, KeyError, ValueError):
        return False


COTTAGE = _is_cottage()


def compile_project(entry: Path, where: Path) -> tuple[Path, dict]:
    """house.json and the areas' manifest of the house at `entry`, written under `where`."""
    where.mkdir(parents=True, exist_ok=True)
    house = where / "house.json"
    house.write_text(housec.document_text(housec.compile_house(str(entry))))
    return house, areasc.compile_areas(str(entry), str(house), str(where / "out"), echo=lambda s: None)


def placements(manifest: dict) -> set:
    """Every (zone, chain, id, key) a manifest places."""
    return {(p["zone"], tuple(c), p["id"], p["key"]) for p in manifest["pieces"] for c in p["chains"]}


def request_from_inside(doc: dict, sha: str) -> dict:
    """A render request for a camera standing in the first indoor room with a ceiling, looking along +x, as
    bridge/client.py sends it to the watcher (its "out" made absolute)."""
    zones = doc["zones"]
    room = next(e for e in doc["elements"] if e["type"] == "space" and e.get("ceiling_z") is not None
                and not zones[e["zone"]].get("outdoor"))
    p = Polygon(room["outline"]).representative_point()
    eye = [p.x, p.y, room["floor_z"] + 60.0]
    cam = Camera.look_at((1600, 1067), eye, [eye[0] + 120.0, eye[1], eye[2]], fov_v_deg=55.0)
    req = make_request(cam, "out/renders/integration_edges.png", hide_tags=default_hide_tags(doc, eye)[0],
                       expect_ir_sha256=sha)
    sent = {}

    class Capture(Bridge):
        def request(self, cmd, *, timeout=None, **fields):
            sent.update(cmd=cmd, **fields)
            return {"state": "ok", "result": {}}

    Capture(ROOT).render(req, expect_sha256=sha)
    assert sent["cmd"] == "render"
    return {"request": sent["request"], "expect_ir_sha256": sent["expect_ir_sha256"]}


# ------------------------------------------------------------------ the sync, under plain Ruby

@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("integration")
    house_a, man_a = compile_project(ROOT / "house.star", tmp / "a")
    out = {"a": man_a, "house": house_a}
    syncs = [man_a, man_a]
    if COTTAGE:
        b = tmp / "b"
        b.mkdir()
        shutil.copy(ROOT / "house.star", b / "house.star")
        for d in ("lib", "zones"):
            shutil.copytree(ROOT / d, b / d)
        design = b / "zones" / "areas" / "kitchen.star"
        text = design.read_text()
        assert text.count(ISLAND_LENGTH[0]) == 1, f"zones/areas/kitchen.star does not say {ISLAND_LENGTH[0]} once"
        design.write_text(text.replace(*ISLAND_LENGTH))
        house_b, man_b = compile_project(b / "house.star", b)
        assert house_b.read_bytes() == house_a.read_bytes()          # an alternative's number leaves today alone
        out["b"] = man_b
        syncs += [man_b, man_a]
    paths = [str(Path(m["plans"]["today"]).parent.parent / "manifest.json") for m in syncs]
    sha = hashlib.sha256(house_a.read_bytes()).hexdigest()
    config = {"build_rb": str(ROOT / "build" / "build.rb"), "syncs": paths, "state": look.STATE,
              "requests": [request_from_inside(json.loads(house_a.read_text()), sha)]}
    (tmp / "config.json").write_text(json.dumps(config))
    r = subprocess.run([RUBY, str(CASES), str(tmp / "config.json")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return {**out, "sha": sha, **json.loads(r.stdout)}


def held(row) -> set:
    return {(z, tuple(c), i, k) for z, c, i, k in row["held"]}


@needs_ruby
def test_a_first_sync_holds_exactly_what_the_manifest_places(run):
    row, man = run["syncs"][0], run["a"]
    want = placements(man)
    assert held(row) == want and len(row["held"]) == len(want)
    pieces = {(p["id"], p["key"]) for p in man["pieces"]}
    assert row["stats"] == {"in_place": False, "built": len(pieces), "instanced": len(want) - len(pieces), "kept": 0,
                            "erased": 0, "placements": len(want)}
    ident = row["identity"]
    assert ident["ir_sha256"] == man["house"]["sha256"] == run["sha"] and ident["manifest_sha256"] == man["sha256"]


@needs_ruby
def test_a_second_sync_of_the_same_manifest_changes_nothing(run):
    row = run["syncs"][1]
    n = len(placements(run["a"]))
    assert row["stats"] == {"in_place": True, "built": 0, "instanced": 0, "kept": n, "erased": 0, "placements": n}
    assert row["redrawn"] == [] and held(row) == held(run["syncs"][0])


@needs_ruby
@pytest.mark.skipif(not COTTAGE, reason="the example cottage's own numbers; house.star is another house")
def test_one_alternatives_number_rebuilds_only_the_pieces_it_reaches(run):
    a, b = placements(run["a"]), placements(run["b"])
    assert {i for _, _, i, _ in a ^ b} == ISLAND_PIECES               # the manifest changed in those pieces alone
    row = run["syncs"][2]
    assert held(row) == b
    assert row["stats"] == {"in_place": True, "built": len(b - a), "instanced": 0, "kept": len(a & b),
                            "erased": len(a - b), "placements": len(b)}
    assert row["redrawn"] == sorted(ISLAND_PIECES)
    back = run["syncs"][3]
    assert held(back) == a and back["stats"]["built"] == len(a - b) and back["redrawn"] == sorted(ISLAND_PIECES)


@needs_ruby
def test_no_sync_changes_what_the_owner_set(run):
    hidden = look.hidden_when_new(run["a"])
    for row in run["syncs"]:
        assert look.owner_changes(row["before"], row["after"], look.HOUSE_TOPS, hidden) == []
        assert row["after"]["active_tag"] == "mine"
    tags = {t["name"]: t["visible"] for t in run["syncs"][0]["after"]["tags"]}
    for area in run["a"]["areas"]:
        assert all(tags[t] == (t == area["today"]) for t in area["tags"])


@needs_ruby
def test_a_photos_render_request_reads_in_render_rb_against_the_synced_house(run):
    got = run["requests"][0]
    assert got["out"] == str(ROOT / "out" / "renders" / "integration_edges.png")
    assert (got["style"], got["projection"]) == ("edges", "perspective") and got["fov"] > 0
    assert all(got["hide_tags"].values()), got["hide_tags"]          # every tag it hides, the sync made
    if COTTAGE:
        assert list(got["hide_tags"]) == ["zone/roof"]               # a camera in a room hides the roof
    assert got["areas_on"] == [a["today"] for a in run["a"]["areas"]]   # a photo's render shows today's house
    assert got["identity"]["ir_sha256"] == run["sha"]                # the house it expects is the one HOUSE records


# ------------------------------------------------------------------ the photo tools' reading of house.json

def references(el):
    """A feature reference to each part of an element a pick may aim at, each pinning a whole point."""
    eid, t = el["id"], el["type"]
    if t == "wall":
        yield {"el": eid, "face": "left", "end": "a", "level": "base"}
        for o in el.get("openings", []):
            yield {"el": o["id"], "face": "right", "jamb": "start", "level": "head"}
            if o.get("unit"):
                yield {"el": o["id"], "face": "left", "jamb": "end", "level": "sill", "light": [0, 0]}
    elif t == "box":
        yield {"el": eid, "x": "min", "y": "max", "z": "max"}
    elif t == "column":
        yield {"el": eid, "x": "centre", "y": "centre", "level": "top"}
    elif t == "beam":
        yield {"el": eid, "side": "left", "end": "b", "level": "soffit"}
    elif t == "stair":
        yield {"el": eid, "riser": el["risers"] - 1, "level": "foot", "end": "a"}
    elif t == "space":
        yield {"el": eid, "vertex": 0, "level": "floor"}
    elif t in ("slab", "roof_plane"):
        yield {"el": eid, "vertex": 0, "level": "bottom"}


def test_every_element_can_be_named_by_a_pick():
    house = features.House.load(HOUSE)
    named = 0
    for el in house.doc["elements"]:
        for ref in references(el):
            got = features.resolve(house, ref)
            assert None not in got.model, (ref, got)
            named += 1
    assert named >= len(house.doc["elements"])


def test_the_photo_tools_see_each_unit_where_build_rb_draws_it():
    doc = json.loads(HOUSE.read_text())
    raw = {e["id"]: e for e in doc["elements"] if e["type"] == "wall"}
    units = 0
    for wall in (e for e in plan_document(doc).elements if e["type"] == "wall"):
        for planned, o in zip(wall["openings"], raw[wall["id"]].get("openings", [])):
            if planned["unit"] is None:
                continue
            cols, rows = features.unit_grid(o, o["at"], o["at"] + o["width"], o["sill_z"], o["head_z"])
            lights = [v for z0, z1 in rows for d0, d1 in cols for v in (d0, d1, z0, z1)]
            assert lights == pytest.approx([v for light in planned["unit"]["lights"] for v in light], abs=0.002), o["id"]
            assert features.unit_plane(raw[wall["id"]], o) == pytest.approx(
                (planned["unit"]["offset"], planned["unit"]["depth"])), o["id"]
            units += 1
    assert units or not any(o.get("unit") for w in raw.values() for o in w.get("openings", []))
