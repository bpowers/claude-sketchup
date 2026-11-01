"""housec: load() inlining, its refusals, error positions mapped back to their files, design layers composed and
applied, and the documents it writes (today's house and a view's).

Tests marked COTTAGE_ONLY hold the example cottage's own numbers and skip once house.star is another house."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import housec

ROOT = Path(__file__).resolve().parent.parent
HOUSE = ROOT / "house.star"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "areas" / "house.star"


def _is_cottage():
    try:
        return json.loads((ROOT / "house.json").read_text())["meta"]["project"]["name"] == "Example cottage"
    except (OSError, KeyError, ValueError):
        return False


COTTAGE_ONLY = pytest.mark.skipif(not _is_cottage(), reason="the example cottage's own test; house.star is another house")


def _tree(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


# ------------------------------------------------------------------ modules

def test_loads_are_inlined_once_each_ahead_of_the_file_that_loads_them(tmp_path):
    _tree(tmp_path, {
        "lib/a.star": 'A = 1\n',
        "lib/b.star": 'load("lib/a.star", "A")\nB = A + 1\n',
        "entry.star": 'load("lib/b.star", "B")\nload("lib/a.star", "A")\n[A, B]\n',
    })
    src = housec.inline(tmp_path / "entry.star")
    assert src.count("A = 1") == 1
    assert src.index("begin lib/a.star") < src.index("begin lib/b.star") < src.index("begin entry.star")
    assert "load(" not in src
    # load lines are blanked, not deleted: each module keeps its own line numbers
    body = src.split("# ---- begin entry.star ----\n")[1]
    assert body.split("\n")[2] == "[A, B]"
    assert housec._eval(src, "entry.star") == [1, 2]


def test_a_name_defined_by_two_modules_is_refused_naming_both(tmp_path):
    _tree(tmp_path, {"lib/a.star": 'X = 1\n', "lib/b.star": 'def X():\n    return 2\n',
                     "entry.star": 'load("lib/a.star", "X")\nload("lib/b.star", "X")\nX\n'})
    with pytest.raises(housec.HousecError, match=r"'X' is defined in both lib/a.star and lib/b.star"):
        housec.inline(tmp_path / "entry.star")
    _tree(tmp_path, {"entry.star": 'UPSTREAM = 1\n'})
    with pytest.raises(housec.HousecError, match="predeclared"):
        housec.inline(tmp_path / "entry.star")


def test_a_circular_or_missing_load_is_refused(tmp_path):
    _tree(tmp_path, {"lib/a.star": 'load("lib/b.star", "B")\nA = 1\n', "lib/b.star": 'load("lib/a.star", "A")\nB = 1\n',
                     "entry.star": 'load("lib/a.star", "A")\nA\n', "lone.star": 'load("lib/none.star", "N")\nN\n'})
    with pytest.raises(housec.HousecError, match="circular load: entry.star -> lib/a.star -> lib/b.star -> lib/a.star"):
        housec.inline(tmp_path / "entry.star")
    with pytest.raises(housec.HousecError, match=r"load target not found: lib/none.star \(from lone.star\)"):
        housec.inline(tmp_path / "lone.star")


def test_an_error_names_the_file_and_line_it_came_from(tmp_path):
    _tree(tmp_path, {
        "lib/a.star": '# a comment\n\ndef check(v):\n    if v > 1:\n        fail("too big: %d" % v)\n    return v\n',
        "lib/b.star": 'load("lib/a.star", "check")\n\nB = check(5)\n',
        "entry.star": 'load("lib/b.star", "B")\nB\n',
    })
    entry = tmp_path / "entry.star"
    with pytest.raises(housec.HousecError) as e:
        housec._eval(housec.inline(entry), "entry.star")
    text = str(e.value)
    assert "lib/a.star:5" in text and "lib/b.star:3" in text and "too big: 5" in text
    assert "entry.star:" not in text
    # the numbers in the quoted snippet's gutter are the file's own too
    assert any(line.strip().startswith("5 |") for line in text.splitlines()), text


def test_an_error_in_a_layer_names_the_layer_or_the_module_it_loads(tmp_path):
    """A layer is evaluated over its frozen loads (housec._eval_layer): an error in the layer's own lines and one in a
    module it loads each name their own file and line."""
    _tree(tmp_path, {
        "house.star": "{}\n",
        "zones/areas.star": '{"AREAS": [{"id": "a", "name": "a"}], "VIEWS": {}}\n',
        "zones/areas/design.star": '\ndef design(v):\n    if v > 1:\n        fail("design rule: %d" % v)\n    return v\n',
        "zones/areas/a/x.star": 'load("zones/areas/design.star", "design")\n\n\nX = design(5)\n',
        "zones/areas/a/y.star": 'load("zones/areas/design.star", "design")\n\n\nY = [][1]\n',
    })
    design = housec.Design(tmp_path / "house.star")
    with pytest.raises(housec.HousecError) as e:
        design.layer("a", "x", {})
    text = str(e.value)
    assert "zones/areas/a/x.star:4" in text and "zones/areas/design.star:4" in text and "#loads" not in text
    assert any(line.strip().startswith("4 |") for line in text.splitlines()), text
    with pytest.raises(housec.HousecError) as e:
        design.layer("a", "y", {})
    text = str(e.value)
    assert "zones/areas/a/y.star:4" in text
    assert any(line.strip().startswith("4 |") for line in text.splitlines()), text


def test_the_dump_source_flag_prints_the_inlined_source(capsys):
    assert housec.main([str(HOUSE), "--dump-source"]) == 0
    out = capsys.readouterr().out
    assert "# ---- begin lib/geometry.star ----" in out and "# ---- begin house.star ----" in out


# ------------------------------------------------------------------ today's document

def test_house_json_is_what_the_sources_compile_to():
    """house.json is committed: it must be today's compile of house.star, byte for byte (housec.py house.star -o
    house.json after any change to the sources)."""
    doc = housec.compile_house(HOUSE)
    assert housec.document_text(doc) == (ROOT / "house.json").read_text()


def test_the_document_frame_is_checked():
    doc = housec.compile_house(HOUSE)
    housec.check_document(doc)
    for mutate, says in [
        (lambda d: d.__setitem__("ir_version", 2), "ir_version 2"),
        (lambda d: d.pop("zones"), "missing top-level keys ['zones']"),
        (lambda d: d["meta"].__setitem__("variant", "other"), "meta.variant is 'other'"),
        (lambda d: d["elements"][0].__setitem__("zone", "attic"), f"{doc['elements'][0]['id']} ('attic')"),
    ]:
        bad = copy.deepcopy(doc)
        mutate(bad)
        with pytest.raises(housec.HousecError) as e:
            housec.check_document(bad)
        assert says in str(e.value)


@COTTAGE_ONLY
def test_the_summary_line_counts_types_openings_and_srcs():
    line = housec.summary(housec.compile_house(HOUSE))
    assert line.startswith("25 elements (beam 1, box 5, column 1, roof_plane 2, slab 1, space 5, stair 1, wall 9)")
    assert "10 openings" in line and "src: cad 25" in line


# ------------------------------------------------------------------ composing layers

def _box(eid, x0=0.0):
    return {"id": eid, "type": "box", "zone": "room", "src": "assumed", "kind": "trim",
            "bbox": [x0, 0.0, 0.0, x0 + 10.0, 10.0, 10.0]}


def _layer(**kw):
    return {"removes": [], "replaces": [], "adds": [], **kw}


@pytest.mark.parametrize("layers, says", [
    ([("a", "x", _layer(adds=[_box("n.box")])), ("b", "p", _layer(adds=[_box("n.box", 5.0)]))],
     "b/p adds n.box, an id a layer before it already uses"),
    ([("a", "x", _layer(removes=["h.box"])), ("b", "p", _layer(replaces=[_box("h.box")]))],
     "b/p replaces h.box, which a layer before it removed"),
    ([("a", "x", _layer(removes=["h.box"])), ("b", "p", _layer(removes=["h.box"]))],
     "b/p removes h.box, which a layer before it already removed"),
    ([("a", "x", _layer(adds=[_box("n.box")])), ("b", "p", _layer(replaces=[{**_box("n.box"), "type": "wall"}]))],
     "a replacement keeps its type"),
])
def test_layers_that_do_not_compose_are_refused(layers, says):
    with pytest.raises(housec.HousecError) as e:
        housec.compose(layers, "trial", "a trial")
    assert says in str(e.value)


def test_a_later_layer_may_replace_and_remove_what_an_earlier_one_added_and_its_checks_win():
    spec = housec.compose([
        ("a", "x", _layer(removes=["h.one"], replaces=[_box("h.two", 1.0)], adds=[_box("n.one"), _box("n.two")],
                          checks=["aisle 44.0 >= 42.0 ok", "seats 3 - - ok"], later=["the stools", "the lights"])),
        ("b", "p", _layer(removes=["n.one", "h.two"], replaces=[_box("n.two", 7.0)], adds=[_box("n.three")],
                          checks=["aisle 43.0 >= 42.0 ok"], drops={"checks": ["seats"], "later": ["the lights"]})),
    ], "trial", "a trial")
    assert spec["removes"] == ["h.one", "h.two"] and spec["replaces"] == []
    assert [(e["id"], e["bbox"][0], e["tags"]) for e in spec["adds"]] == [("n.two", 7.0, ["b/p"]),
                                                                         ("n.three", 0.0, ["b/p"])]
    assert spec["checks"] == ["aisle 43.0 >= 42.0 ok"] and spec["later"] == ["the stools"]
    assert spec["layers"] == [{"area": "a", "alt": "x"}, {"area": "b", "alt": "p"}]


def _base():
    return {"ir_version": 1, "meta": {"variant": "today", "project": {"name": "t"}},
            "zones": {"room": {"storey": 0}}, "datums": {"floor": 0.0},
            "elements": [_box("k.box.one"), _box("k.box.two", 20.0), _box("k.box.three", 40.0)]}


def _spec(**kw):
    return {"id": "trial", "name": "a trial", "removes": [], "replaces": [], "adds": [],
            "layers": [{"area": "kitchen", "alt": "trial"}], **kw}


def test_apply_option_keeps_order_tags_what_it_touches_and_leaves_the_base_alone():
    base = _base()
    before = copy.deepcopy(base)
    spec = housec.compose([("kitchen", "trial", _layer(removes=["k.box.one"], replaces=[_box("k.box.two", 25.0)],
                                                       adds=[_box("trial.box.new", 60.0)]))], "trial", "a trial")
    doc = housec.apply_option(base, spec, "trial", "ab" * 32, "cd" * 32)
    assert base == before
    assert [e["id"] for e in doc["elements"]] == ["k.box.two", "k.box.three", "trial.box.new"]
    by = {e["id"]: e for e in doc["elements"]}
    assert by["k.box.two"]["tags"] == ["kitchen/trial"] and by["trial.box.new"]["tags"] == ["kitchen/trial"]
    assert by["k.box.three"] == before["elements"][2]
    opt = doc["meta"]["option"]
    assert (opt["removes"], opt["replaces"], opt["adds"]) == (["k.box.one"], ["k.box.two"], ["trial.box.new"])
    assert opt["base"] == {"variant": "today", "sha256": "ab" * 32, "elements": 3} and opt["source_sha256"] == "cd" * 32
    assert doc["meta"]["variant"] == "trial" and doc["zones"] == base["zones"] and doc["datums"] == base["datums"]
    housec.check_document(doc, "trial")


@pytest.mark.parametrize("spec, says", [
    (_spec(extra=1), "unknown key 'extra'"),
    ({k: v for k, v in _spec().items() if k != "layers"}, "missing key 'layers'"),
    (_spec(id="other"), "its id is 'other'"),
    (_spec(removes=["k.box.nine"]), "removes 'k.box.nine', which is not an element"),
    (_spec(removes=["k.box.three", "k.box.three"]), "removes k.box.three twice"),
    (_spec(replaces=[_box("k.box.nine")]), "replaces 'k.box.nine', which is not an element"),
    (_spec(removes=["k.box.three"], replaces=[_box("k.box.three")]), "both removes and replaces k.box.three"),
    (_spec(replaces=[{**_box("k.box.one"), "type": "slab"}]), "a replacement keeps its type"),
    (_spec(adds=[_box("k.box.three")]), "an id today's house already uses"),
    (_spec(adds=[_box("trial.box.x"), _box("trial.box.x")]), "adds trial.box.x twice"),
])
def test_apply_option_refuses_what_does_not_apply(spec, says):
    with pytest.raises(housec.HousecError) as e:
        housec.apply_option(_base(), spec, "trial", "ab" * 32)
    assert says in str(e.value)


# ------------------------------------------------------------------ design areas

def test_a_layer_that_refuses_with_a_choice_code_is_unavailable_and_any_other_failure_is_an_error():
    design = housec.Design(FIXTURE)
    layers, missing = design.stack({"b": "p"})
    assert layers == [] and missing["b"].code == "choice.no_a" and missing["b"].design == "fixture"
    assert missing["b"].words == "p stands in x or y"
    with pytest.raises(housec.HousecError, match="b/p is not available here: fixture choice.no_a"):
        design.stack({"b": "p"}, strict=True)
    layers, missing = design.stack({"a": "y", "b": "p"})
    assert [(a, alt) for a, alt, _ in layers] == [("a", "y"), ("b", "p")] and not missing
    assert layers[1][2]["adds"][0]["bbox"][0] == 220.0               # p stands where y's export says
    with pytest.raises(housec.HousecError, match="no area z"):
        design.stack({"z": "q"})
    with pytest.raises(housec.HousecError, match="no view 'nope'"):
        housec.evaluate_option(FIXTURE, "nope")
    # only a choice code says an alternative cannot stand here: a design rule's own refusal is an error everywhere
    for words in ("p stands nowhere", "fixture aisle: 30 under 42"):
        broken = housec.Design(FIXTURE, rewrite=lambda src, w=words: src.replace(
            "fixture choice.no_a: p stands in x or y", w))
        with pytest.raises(housec.HousecError, match="evaluation failed"):
            broken.stack({"b": "p"})


@pytest.mark.parametrize("entry", [HOUSE, FIXTURE])
def test_a_layer_evaluated_over_its_frozen_loads_is_the_layer_evaluated_whole(entry):
    design = housec.Design(entry)
    reads = {a["id"]: a["reads"] for a in design.areas}
    for combo in design.combinations():
        layers, _ = design.stack(combo)
        exports = {a: {"alt": "today"} for a in design.alts}
        for area, alt, spec in layers:
            src, rel = design.source(area, alt)
            assert spec == housec._eval(src, rel, {r: exports[r] for r in reads[area]})["LAYER"], (area, alt)
            exports[area] = {**spec.get("exports", {}), "alt": alt}


@pytest.mark.parametrize("text, says", [
    ('{"LAYER": {"id": "q", "name": "q", "removes": [], "replaces": [], "adds": []}}', "its id is 'q'"),
    ('{"LAYER": {"id": "x", "name": "x", "removes": [], "replaces": [], "adds": [], "colour": 1}}',
     "unknown key 'colour'"),
    ('{"LAYER": {"id": "x", "name": "x", "removes": [], "replaces": []}}', "missing key 'adds'"),
    ('{"LAYER": {"id": "x", "name": "x", "removes": [], "replaces": [], "adds": [], "exports": {"alt": 1}}}',
     "exports.alt"),
    ('{"id": "x"}', 'must end with the expression {"LAYER"'),
])
def test_a_layer_of_the_wrong_shape_is_refused(tmp_path, text, says):
    _tree(tmp_path, {"house.star": "{}\n", "zones/areas.star": '{"AREAS": [{"id": "a", "name": "a"}], "VIEWS": {}}\n',
                     "zones/areas/a/x.star": text + "\n"})
    with pytest.raises(housec.HousecError) as e:
        housec.Design(tmp_path / "house.star").layer("a", "x", {})
    assert says in str(e.value)


def test_areas_and_views_that_name_what_is_not_there_are_refused(tmp_path):
    _tree(tmp_path, {"house.star": "{}\n", "zones/areas/a/x.star": "{}\n", "zones/areas.star": (
        '{"AREAS": [{"id": "a", "name": "a"}, {"id": "b", "name": "b", "reads": ["c"]}],'
        ' "VIEWS": {"v": {"name": "v", "choose": {"a": "y"}}, "w": {"name": "w", "choose": {"z": "x"}}}}\n')})
    with pytest.raises(housec.HousecError) as e:
        housec.Design(tmp_path / "house.star")
    text = str(e.value)
    assert "area b reads ['c']" in text and "view v chooses a/y" in text and "view w chooses for 'z'" in text


def test_a_house_without_areas_has_no_views(tmp_path):
    _tree(tmp_path, {"house.star": "{}\n"})
    design = housec.Design(tmp_path / "house.star")
    assert design.areas == [] and design.views == {} and design.combinations() == [{}]


# ------------------------------------------------------------------ a view's document

@COTTAGE_ONLY
def test_the_option_flag_writes_a_views_whole_house_document(tmp_path, capsys):
    out = tmp_path / "kitchen_island.json"
    assert housec.main([str(HOUSE), "--option", "kitchen_island", "-o", str(out)]) == 0
    assert "view kitchen_island: removes 2, replaces 3, adds 2" in capsys.readouterr().out
    doc = json.loads(out.read_text())
    housec.check_document(doc, "kitchen_island")
    opt = doc["meta"]["option"]
    assert opt["layers"] == [{"area": "kitchen", "alt": "island"}]
    assert opt["base"]["sha256"] == hashlib.sha256((ROOT / "house.json").read_bytes()).hexdigest()
    assert opt["source_sha256"] == housec.option_source_sha256(HOUSE, "kitchen_island")
    assert [c.split(" ")[0] for c in opt["checks"]] == ["aisle", "walkway.stools", "walkway.west_end",
                                                         "walkway.east_end", "counter.length", "seats"]
    by = {e["id"]: e for e in doc["elements"]}
    assert "kitchen.upper.left" not in by and by["kitchen.island.counter"]["tags"] == ["kitchen/island"]
    assert by["kitchen.fridge"]["tags"] == ["kitchen/island"] and "tags" not in by["living.space"]
    assert doc["zones"] == json.loads((ROOT / "house.json").read_text())["zones"]


@COTTAGE_ONLY
def test_a_design_that_breaks_its_rule_is_refused_with_its_code():
    """Moving one of the kitchen design's numbers past a rule refuses the view with that rule's code."""
    for setting, code in [("BLOCK_WALKWAY = 46.0", "aisle"), ("BLOCK_WALKWAY = 30.0", "walkway.stools"),
                          ("ISLAND_END_WALKWAY = 30.0", "walkway.west_end"), ("ISLAND_LENGTH = 150.0",
                                                                               "walkway.east_end")]:
        name = setting.split(" ")[0]
        design = housec.Design(HOUSE, rewrite=lambda src, s=setting, n=name: "\n".join(
            s if line.startswith(n + " = ") else line for line in src.split("\n")))
        with pytest.raises(housec.HousecError) as e:
            housec.evaluate_option(HOUSE, "kitchen_island", design=design)
        assert f"kitchen {code}:" in str(e.value), (setting, str(e.value)[-300:])
