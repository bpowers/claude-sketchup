"""The Ruby side without SketchUp: every .rb file parses, and build.rb's and render.rb's bookkeeping runs under plain
Ruby against a stand-in for SketchUp's API (tests/ruby/sketchup_stub.rb, driven by tests/ruby/build_cases.rb).

What is held here: reading the palette, plans and manifests (and refusing malformed ones); materials set only where
they differ; a sync placing pieces under their tag chains in place (kept, instanced, built and erased exactly as the
manifest changed, empty containers pruned, containers moved by hand rebuilt and untagged ones re-tagged),
alternatives' tags created hidden and today's shown, the owner's tag choices and active tag kept, everything drawn on
Layer0; a build into a test's top group touching nothing else; choose; render.rb's request parsing, area switching and
camera refusal; the crease pass's arithmetic on plain face data. And the real watcher.rb, served under plain Ruby in a
copy of the project (tests/ruby/watcher_serve.rb), answering the real client.py: one protocol, errors with their
element, a request sent before the watcher starts, two requests under one mtime, install!'s loader, and the smoke
check's clean-up. SketchUp's geometry is not held here: bridge/smoke.py checks that live.

    uv run pytest tests/test_ruby.py          # skips without Ruby 3.2 or later on the PATH (tests/rubyenv.py)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ir import plan as irplan
from rubyenv import RUBY, needs_ruby

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "ruby" / "build_cases.rb"
SAMPLE = ROOT / "build" / "fixtures" / "sample.json"
RB_FILES = sorted([*ROOT.glob("build/*.rb"), *ROOT.glob("bridge/*.rb"), *ROOT.glob("tests/ruby/*.rb")])

pytestmark = needs_ruby


@pytest.mark.parametrize("path", RB_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_ruby_file_parses(path):
    r = subprocess.run([RUBY, "-c", str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "Syntax OK" in r.stdout, r.stderr


def key(el) -> str:
    return hashlib.sha256(json.dumps(el, sort_keys=True).encode()).hexdigest()


def write_plan(doc, path: Path) -> tuple[Path, list]:
    path.with_suffix(".src.json").write_text(json.dumps(doc))
    p = irplan.plan_document(doc)
    assert p.ok, p.message()
    return irplan.write_plan(p, path.with_suffix(".src.json"), "0" * 64, path), p.elements


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ruby")
    sample = json.loads(SAMPLE.read_text())
    today, els = write_plan(sample, tmp / "today.json")
    island_doc = json.loads(SAMPLE.read_text())
    island_doc["zones"]["kitchen"] = {"storey": 0}
    island_doc["elements"].append({"id": "sample.box.island", "type": "box", "zone": "kitchen", "src": "assumed",
                                   "bbox": [40, 40, 0, 80, 64, 36], "kind": "base_cabinet"})
    island, island_els = write_plan(island_doc, tmp / "island.json")
    by = {e["id"]: e for e in island_els}
    kitchen = ["sample.base.run", "sample.counter.run"]

    def piece(eid, chains, plan="today", k=None):
        return {"id": eid, "key": k or key(by[eid]), "plan": plan, "zone": by[eid]["zone"], "type": by[eid]["type"],
                "chains": chains}

    def manifest(pieces, name):
        m = {"manifest": 1, "house": {"path": str(tmp / "house.json"), "sha256": "0" * 64},
             "areas": [{"id": "kitchen", "name": "Kitchen", "tags": ["kitchen/today", "kitchen/island"],
                        "today": "kitchen/today"}],
             "views": {"island": {"name": "Island", "tags": ["kitchen/island"]}},
             "plans": {"today": str(today), "island": str(island)}, "pieces": pieces, "unavailable": [],
             "location": sample["location"], "sha256": name * 64}
        path = tmp / f"manifest_{name}.json"
        path.write_text(json.dumps(m))
        return path

    # a: every element of today's but the kitchen run in the shell, the run under kitchen/today, and the island's box
    # under kitchen/island. b: the east wall changed, the post moved from the shell into kitchen/today, the counter
    # also shown under kitchen/island, and the island's box gone.
    a = [piece(e["id"], [[]]) for e in els if e["id"] not in kitchen] + \
        [piece(i, [["kitchen/today"]]) for i in kitchen] + [piece("sample.box.island", [["kitchen/island"]], "island")]
    b = [piece(e["id"], [[]], k="changed" if e["id"] == "sample.wall.east" else None) for e in els
         if e["id"] not in kitchen + ["sample.column.post"]] + \
        [piece("sample.base.run", [["kitchen/today"]]), piece("sample.column.post", [["kitchen/today"]]),
         piece("sample.counter.run", [["kitchen/today"], ["kitchen/island"]])]
    bad_palettes, bad_manifests = [], []
    for name, body in (("no_placeholder", {"drywall": {"rgb": [1, 2, 3]}}),
                       ("bad_rgb", {"placeholder": {"rgb": [1, 2]}})):
        bad_palettes.append(str(tmp / f"palette_{name}.json"))
        Path(bad_palettes[-1]).write_text(json.dumps(body))
    good = json.loads(manifest(a, "a").read_text())
    for name, change in (("version", {"manifest": 2}),
                         ("today", {"areas": [{"id": "kitchen", "tags": ["kitchen/today"], "today": "kitchen/x"}]}),
                         ("piece", {"pieces": [{"id": "sample.space.room", "zone": "living", "plan": "today"}]})):
        bad_manifests.append(str(tmp / f"manifest_bad_{name}.json"))
        Path(bad_manifests[-1]).write_text(json.dumps({**good, **change}))
    config = {"build_rb": str(ROOT / "build" / "build.rb"), "palette": str(ROOT / "build" / "palette.json"),
              "plan": str(today), "house_json": str(SAMPLE), "bad_palettes": bad_palettes,
              "bad_manifests": bad_manifests,
              "manifests": {"a": str(manifest(a, "a")), "b": str(manifest(b, "b"))}}
    (tmp / "config.json").write_text(json.dumps(config))
    r = subprocess.run([RUBY, str(CASES), str(tmp / "config.json")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout)


def test_the_palette_is_read_and_materials_are_set_only_where_they_differ(out):
    p = out["palette"]
    assert p["keys"] == len(json.loads((ROOT / "build" / "palette.json").read_text()))
    assert p["glass_alpha"] == 0.18 and p["drywall_rgb"] == [218, 215, 209] and p["color_sets"] == 1
    no_placeholder, bad_rgb = p["bad"]
    assert "must be an object of finishes" in no_placeholder and "needs \"rgb\": [r, g, b]" in bad_rgb


def test_plans_are_read_with_symbol_keys_and_a_house_document_is_refused(out):
    p = out["plan"]
    assert p["elements"] == 12 and p["first_keys"] == [":id", ":type", ":zone", ":src"]
    assert p["location"]["city"] == "Example Town" and "porch" in p["zones"]
    assert "is not a plan of version 1" in p["not_a_plan"] and "no plan at /nonexistent/plan.json" in p["missing"]


def test_malformed_manifests_are_refused(out):
    m = out["manifest"]
    assert m["ok"] == 1
    version, today, piece = m["bad"]
    assert "not a manifest of version 1" in version
    assert "today among its tags" in today and "{id, key, plan, zone, chains}" in piece


def test_a_first_sync_builds_every_placement_with_area_tags_today_shown(out):
    row = out["sync"]["first"]
    assert row["stats"] == {"in_place": False, "built": 13, "instanced": 0, "kept": 0, "erased": 0, "placements": 13}
    assert row["tags"]["kitchen/today"] is True and row["tags"]["kitchen/island"] is False
    assert row["active"] == "mine" and row["created_on"] == ["Layer0"]
    assert "zone/living [zone/living] > kitchen/today [kitchen/today] > sample.base.run [type/box]" in row["tree"]
    assert "zone/kitchen [zone/kitchen] > kitchen/island [kitchen/island] > sample.box.island [type/box]" in row["tree"]
    assert "zone/living [zone/living] > sample.space.room [type/space]" in row["tree"]
    ident = row["identity"]
    assert ident["manifest_sha256"] == "a" * 64 and ident["elements"] == 12 and len(ident["build_sha256"]) == 64
    assert row["location"]["City"] == "Example Town" and row["location"]["NorthAngle"] == 0.0
    assert row["ops"][-1] == "commit"


def test_a_sync_after_no_change_changes_nothing(out):
    row = out["sync"]["again"]
    assert row["stats"] == {"in_place": True, "built": 0, "instanced": 0, "kept": 13, "erased": 0, "placements": 13}
    assert row["tree"] == out["sync"]["first"]["tree"]


def test_a_sync_builds_instances_and_erases_exactly_what_changed(out):
    row = out["sync"]["changed"]
    # the east wall's key changed: built, its old group erased; the post moved into a chain: instanced from its
    # shell group, which is erased; the counter shown under a second chain: instanced; the island's box: erased
    assert row["stats"] == {"in_place": True, "built": 1, "instanced": 2, "kept": 10, "erased": 3, "placements": 13}
    assert not any(t.startswith("zone/kitchen") for t in row["tree"])          # its emptied containers are pruned
    assert "zone/living [zone/living] > kitchen/today [kitchen/today] > sample.column.post [type/column]" in row["tree"]
    assert "zone/living [zone/living] > kitchen/island [kitchen/island] > sample.counter.run [type/box]" in row["tree"]
    # the owner's switch stands, and nothing was drawn on their current tag
    assert row["tags"]["kitchen/island"] is True and row["tags"]["kitchen/today"] is False
    assert row["active"] == "mine" and row["created_on"] == ["Layer0"]
    back = out["sync"]["back"]
    assert back["stats"] == {"in_place": True, "built": 2, "instanced": 1, "kept": 10, "erased": 3, "placements": 13}
    assert sorted(back["tree"]) == sorted(out["sync"]["first"]["tree"])


def test_a_sync_undoes_what_the_owner_did_by_hand_to_its_containers(out):
    # a zone group moved by hand is erased and its piece built again; a chain container left untagged (its tag deleted
    # in SketchUp) is put back on its tag; a chain container moved by hand is erased and its pieces built again
    first = sorted(out["sync"]["first"]["tree"])
    settled = out["sync"]["settled"]
    assert settled["stats"]["built"] == 1 and settled["stats"]["kept"] == 12 and settled["moved_containers"] == 1
    assert sorted(settled["tree"]) == first
    moved = out["sync"]["moved_chain"]
    assert moved["stats"]["built"] == 2 and moved["stats"]["kept"] == 11 and moved["moved_containers"] == 1
    assert sorted(moved["tree"]) == first


def test_rebuild_draws_everything_again(out):
    row = out["sync"]["rebuild"]
    assert row["stats"]["in_place"] is False and row["stats"]["built"] == 13
    assert sorted(row["tree"]) == sorted(out["sync"]["first"]["tree"])


def test_no_build_draws_into_the_furnishings(out):
    assert "FURNISHINGS is the furniture's group" in out["sync"]["furnishings"]


def test_a_build_into_a_test_top_touches_nothing_else(out):
    r = out["run"]
    assert r["tops"] == ["HOUSE", "TEST top"] and r["house_kept"]
    assert (r["elements"], r["built"], r["in_place"]) == (12, 12, False)
    assert r["location"] == {} and r["reports"] == []
    assert r["test_tree"][0] == "zone/living [zone/living]"


def test_choose_switches_an_area_in_one_step(out):
    c = out["choose"]
    assert c["on"] == {"kitchen": "kitchen/today"}
    assert c["tags"] == {"kitchen/today": True, "kitchen/island": False}
    assert c["ops"] == ["start House twin: choose design areas", "commit"]
    assert "no design area garage" in c["unknown_area"] and "kitchen has no alternative galley" in c["unknown_alt"]
    assert c["agree"] == [True, False, True]


def test_render_requests_are_parsed_and_checked(out):
    r = out["render_read"]
    assert r["ok"] == {"projection": "perspective", "fov": 55.0, "style": "edges", "transparent": False,
                       "hide_tags": ["zone/roof"], "areas": ["kitchen/island"], "top": "HOUSE", "crop": [0, 0, 10, 10]}
    assert r["parallel"] == {"fov": None, "view_height": 400.0}
    words = ["absolute path to a .png", "width and height must each lie in 1..16384", "'fov_v_deg' is missing",
             "'view_height' is missing", "'style' is \"wireframe\"", "eye and target coincide",
             "'up' is parallel to the view direction", "64 lowercase hex", "'hide_tags' must be a list of strings"]
    assert len(r["errors"]) == len(words)
    for got, want in zip(r["errors"], words):
        assert got and want in got, (want, got)


def test_render_switches_each_area_to_one_tag(out):
    a = out["render_areas"]
    assert a["none"] == [["kitchen/today"], ["kitchen/island"]]
    assert a["named"] == [["kitchen/island"], ["kitchen/today"]]
    assert "garage/today, which is no area tag" in a["unknown"]
    assert "two alternatives of kitchen" in a["two"]
    assert a["other_top"] == [[], []]


def test_a_field_of_view_sketchup_reports_across_the_width_is_refused(out):
    f = out["fov"]
    assert f["height"] == {"fov": 55.0, "fov_is_height": True}
    assert "across the image width" in f["width"]


def test_crease_interval_arithmetic(out):
    i = out["intervals"]
    assert i["intersect"] == [[5.0, 10.0], [20.0, 25.0]]
    assert i["subtract"] == [[0.0, 2.0], [4.0, 9.0]]
    assert i["merge"] == [[[0.0, 0.0, 0.0], [9.0, 0.0, 0.0]], [[0.0, 1.0, 0.0], [1.0, 1.0, 0.0]]]
    assert i["fmt"] == ["35", "0", "1.235"]


def test_creases_are_where_two_solids_meet_and_nowhere_else(out):
    c = out["creases"]
    assert c["ceiling"] == [[[0.0, 0.0, 90.0], [0.0, 100.0, 90.0]], [[6.0, 0.0, 90.0], [6.0, 100.0, 90.0]]]
    assert c["cross"] == [[[x, y, 0.0], [x, y, 96.0]] for x in (47.0, 53.0) for y in (0.0, 6.0)]
    assert c["tee"] == []          # the stub wall's own end edges, in the host's face, already draw both corners
    assert c["apart"] == []


def copy_project(tmp_path):
    """The files the watcher serves, copied under tmp_path, whose paths then come from there."""
    for rel in ("bridge/watcher.rb", "build/build.rb", "build/render.rb", "build/palette.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)


@pytest.fixture
def watcher(tmp_path):
    """The real watcher.rb, served under plain Ruby in a temporary copy of the project (tests/ruby/watcher_serve.rb);
    yields that project's root."""
    copy_project(tmp_path)
    proc = subprocess.Popen([RUBY, str(ROOT / "tests" / "ruby" / "watcher_serve.rb"), str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        yield tmp_path
    finally:
        (tmp_path / "stop").write_text("")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_client_and_the_watcher_speak_one_protocol(watcher):
    from bridge.client import Bridge, BridgeError
    br = Bridge(watcher, activate=False, max_pickup=20, timeout=30)
    info = br.ping()
    assert info["protocol"] == 1 and info["project"] == str(watcher) and info["model"] == "stand-in"
    assert br.eval("[1 + 1, :sym, nil]") == [2, "sym", None]
    assert br.request("eval", code="x = 41")["eval"] == "41" and br.eval("x + 1") == 42   # one binding throughout
    assert br.built() is None                                     # no HOUSE in the stand-in model
    with pytest.raises(BridgeError) as info:
        br.request("dance")
    assert info.value.ruby_class == "ArgumentError" and "unknown bridge command" in str(info.value)
    with pytest.raises(BridgeError) as info:
        br.build(SAMPLE)                                          # planned here, drawn there, fails at the first face
    e = info.value
    assert e.ruby_class == "HouseTwin::BuildError" and e.element_id == "sample.space.room" and e.backtrace
    status = json.loads((watcher / "out" / "status.json").read_text())
    assert status["state"] == "error" and status["cmd"] == "build"
    br.eval("Sketchup.active_model.path = '/elsewhere/other.skp'")
    with pytest.raises(BridgeError, match="will not write another model over the project's file"):
        br.save()


def test_a_request_under_the_last_ones_mtime_is_still_picked_up(watcher):
    """On a filesystem with coarse time stamps (FAT32, HFS+) two requests written within a second share an mtime; the
    watcher tells them apart by id."""
    from bridge.client import Bridge
    br = Bridge(watcher, activate=False, max_pickup=20, timeout=30)
    br.ping()
    stamp = br.trigger.stat()
    tmp = br.trigger.with_name("trigger.same-mtime")
    tmp.write_text(json.dumps({"bridge": 1, "id": "same-mtime-1", "cmd": "eval", "code": "6 * 7"}))
    os.utime(tmp, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.replace(tmp, br.trigger)
    reply = br.replies / "same-mtime-1.json"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (reply.is_file() and '"ok"' in reply.read_text()):
        time.sleep(0.02)
    assert json.loads(reply.read_text())["result"] == 42


def test_the_smoke_checks_clean_up_takes_only_what_its_builds_made(watcher):
    from bridge import smoke
    from bridge.client import Bridge
    br = Bridge(watcher, activate=False, max_pickup=20, timeout=30)
    br.eval("HouseTwin::Build.tag(Sketchup.active_model, 'zone/living'); Sketchup.active_model.layers.add('mine'); nil")
    before = br.eval(smoke.NAMES)
    br.eval("HouseTwin::Build.tag(Sketchup.active_model, 'zone/loft'); HouseTwin::Build.tag(Sketchup.active_model, "
            "'type/wall'); g = Sketchup.active_model.entities.add_group; g.name = 'SMOKE'; nil")
    done = br.eval(smoke.clean_up(before))
    assert done == {"erased": 1, "tags": ["zone/loft", "type/wall"], "folders": ["type"]}
    after = br.eval(smoke.NAMES)
    assert after == before and br.eval("Sketchup.active_model.entities.count") == 0


def test_install_writes_a_loader_that_loads_this_project(watcher):
    from bridge.client import Bridge
    br = Bridge(watcher, activate=False, max_pickup=20, timeout=30)
    plugins = watcher / "Plugins"
    plugins.mkdir()
    loader = br.eval(f"def Sketchup.find_support_file(_) = {json.dumps(str(plugins))}; HouseTwin::Watcher.install!")
    text = Path(loader).read_text()
    assert Path(loader).parent == plugins and f'HOUSE_TWIN_PROJECT = "{watcher}"' in text
    r = subprocess.run([RUBY, "-c", loader], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_a_request_sent_before_the_watcher_starts_is_answered_once(tmp_path):
    """A client may write its request while SketchUp is still starting; the watcher answers it when it starts. A request
    it has already picked up is never run again by a later start."""
    from bridge.client import Bridge
    copy_project(tmp_path)
    br = Bridge(tmp_path, activate=False, max_pickup=20, timeout=30)
    br.replies.mkdir(parents=True)
    br._write_trigger(json.dumps({"bridge": 1, "id": "early-1", "cmd": "eval", "code": "$runs = ($runs || 0) + 1"}))

    def serve_once():
        started = time.time()
        proc = subprocess.Popen([RUBY, str(ROOT / "tests" / "ruby" / "watcher_serve.rb"), str(tmp_path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20                         # until this session has written its status
        while time.monotonic() < deadline and not (br.status.is_file() and br.status.stat().st_mtime > started):
            time.sleep(0.02)
        time.sleep(0.3)
        (tmp_path / "stop").write_text("")
        proc.wait(timeout=10)
        (tmp_path / "stop").unlink()
    serve_once()
    reply = json.loads((br.replies / "early-1.json").read_text())
    assert reply["state"] == "ok" and reply["result"] == 1
    (br.replies / "early-1.json").unlink()                      # as the client does once it has read it
    serve_once()
    assert not (br.replies / "early-1.json").exists()           # not run again
