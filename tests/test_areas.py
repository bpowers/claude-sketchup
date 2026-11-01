"""areasc: the design areas' manifest, which SketchUp is synced from.

The promise held here, for the house and for a two-area design under tests/fixtures/areas/: whatever tags are on, one
per area, the pieces whose chains are all on are exactly that combination's house, each element once, with the key it
has there. Beside it: the chains of shell pieces and switched pieces, keys shared between alternatives that keep an
element, the views' tags, alternatives that cannot stand where they read, and areas composed apart only where they
stand apart.

Tests marked COTTAGE_ONLY hold the example cottage's own design and skip once house.star is another house.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import random
import shutil
from pathlib import Path

import pytest

import areasc
import housec

ROOT = Path(__file__).resolve().parent.parent
HOUSE = ROOT / "house.star"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "areas"
SPEC_KEYS = {"manifest", "house", "areas", "views", "plans", "pieces", "unavailable", "location", "sha256"}


def _is_cottage():
    try:
        return json.loads((ROOT / "house.json").read_text())["meta"]["project"]["name"] == "Example cottage"
    except (OSError, KeyError, ValueError):
        return False


COTTAGE_ONLY = pytest.mark.skipif(not _is_cottage(), reason="the example cottage's own test; house.star is another house")


# ------------------------------------------------------------------ cover

DIMS = [("a", ["today", "x", "y"]), ("b", ["today", "p"]), ("c", ["today", "m", "n", "o"])]
ALL = list(itertools.product(*[v for _, v in DIMS]))


def _on(chain, combo):
    return all(combo[[a for a, _ in DIMS].index(area)] == alt for area, alt in chain.items())


@pytest.mark.parametrize("seed", range(40))
def test_cover_shows_a_piece_in_exactly_its_combinations_and_never_twice(seed):
    rng = random.Random(seed)
    present = {t for t in ALL if rng.random() < rng.choice([0.1, 0.5, 0.9])}
    chains = areasc.cover(present, DIMS)
    for combo in ALL:
        assert sum(_on(c, combo) for c in chains) == (1 if combo in present else 0), (combo, chains)


def test_cover_names_only_the_areas_that_make_a_difference():
    assert areasc.cover(set(ALL), DIMS) == [{}]                                  # the shell: one empty chain
    assert areasc.cover(set(), DIMS) == []
    assert areasc.cover({t for t in ALL if t[0] == "x"}, DIMS) == [{"a": "x"}]
    assert areasc.cover({t for t in ALL if t[2] == "n"}, DIMS) == [{"c": "n"}]
    assert areasc.cover({t for t in ALL if t[0] == "x" and t[1] == "p"}, DIMS) == [{"a": "x", "b": "p"}]
    assert sorted(areasc.cover({t for t in ALL if t[0] in ("x", "y")}, DIMS), key=str) == [{"a": "x"}, {"a": "y"}]
    assert areasc.chain_tags({"c": "n", "a": "x"}, ["a", "b", "c"]) == ["a/x", "c/n"]


# ------------------------------------------------------------------ the proof

def _shown(manifest, on):
    """{element id: [the keys showing]} while exactly the tags `on` are on."""
    out = {}
    for p in manifest["pieces"]:
        for chain in p["chains"]:
            if set(chain) <= on:
                out.setdefault(p["id"], []).append(p["key"])
    return out


def _holds(entry, house_json, manifest):
    """Every combination of the design shows exactly the house it composes to; returns how many were checked. Each is
    composed whole here, which the compile never does across its groups: this holds the groups to standing apart.
    Past areasc.PROOF_LIMIT combinations it holds each area's alternatives alone and a sample of the rest."""
    design = housec.Design(entry)
    raw = Path(house_json).read_bytes()
    base, sha = json.loads(raw), hashlib.sha256(raw).hexdigest()
    layer_tags = {t for a in manifest["areas"] for t in a["tags"]}
    combos = areasc.proof_combinations(design, [[a] for a in design.alts])
    for combo in combos:
        layers, _ = design.stack(combo)
        doc = housec.apply_option(base, housec.compose(layers, "c", "c"), "c", sha) if layers else base
        keys, hollow, _ = areasc.planned_keys(doc, layer_tags)
        want = {i: [k] for i, k in keys.items() if i not in hollow}
        on = {housec.layer_tag(a, alt) for a, alt in combo.items()}
        assert _shown(manifest, on) == want, combo
    return len(combos)


def _compile(entry, out, house=None):
    """Compile `entry`'s house.json (into `house`, beside the entry by default) and its areas into `out`."""
    house = Path(house or Path(entry).parent / "house.json")
    house.write_text(housec.document_text(housec.compile_house(entry)))
    return areasc.compile_areas(str(entry), str(house), str(out), echo=lambda s: None), house


def _chains(manifest, eid, today=None):
    """The chains the pieces of element `eid` show under; with `today`, only those of today's piece (True) or of the
    alternatives' (False)."""
    return sorted(c for p in manifest["pieces"] if p["id"] == eid and today in (None, p["plan"] == "today")
                  for c in p["chains"])


# ------------------------------------------------------------------ the house

@pytest.fixture(scope="module")
def cottage(tmp_path_factory):
    """The house's manifest, compiled from the committed house.json into a directory of its own."""
    out = tmp_path_factory.mktemp("cottage")
    lines = []
    manifest = areasc.compile_areas(str(HOUSE), str(ROOT / "house.json"), str(out), echo=lines.append)
    return manifest, out, lines


def test_the_manifest_has_the_spec_shape(cottage):
    manifest, out, lines = cottage
    assert set(manifest) == SPEC_KEYS and manifest["manifest"] == 1
    assert manifest["sha256"] == areasc._sha(areasc.canonical({k: v for k, v in manifest.items() if k != "sha256"}))
    assert json.loads((out / "areas" / "manifest.json").read_text()) == manifest
    assert manifest["house"] == {"path": str(ROOT / "house.json"),
                                 "sha256": hashlib.sha256((ROOT / "house.json").read_bytes()).hexdigest()}
    assert {p["plan"] for p in manifest["pieces"]} == set(manifest["plans"]) | {"today"}
    for name, path in manifest["plans"].items():
        plan = json.loads(Path(path).read_text())
        assert plan["plan"] == 1 and Path(plan["ir"]).is_file(), name
        assert plan["ir_sha256"] == hashlib.sha256(Path(plan["ir"]).read_bytes()).hexdigest()
    assert all(set(p) == {"id", "key", "plan", "zone", "type", "chains"} for p in manifest["pieces"])
    tags = {t for a in manifest["areas"] for t in a["tags"]}
    assert all(set(v["tags"]) <= tags and len(v["tags"]) == len(manifest["areas"]) for v in manifest["views"].values())
    assert lines[0].startswith("wrote ")


def test_every_combination_of_the_house_shows_exactly_what_it_composes_to(cottage):
    manifest, _, _ = cottage
    assert _holds(HOUSE, ROOT / "house.json", manifest) >= 1


def test_todays_tags_show_todays_house(cottage):
    manifest, _, _ = cottage
    base = json.loads((ROOT / "house.json").read_text())
    today = {a["today"] for a in manifest["areas"]}
    todays = [p for p in manifest["pieces"] if p["plan"] == "today"]
    assert all(set(p["chains"][0]) <= today for p in todays)            # today's chain first
    keys, hollow, _ = areasc.planned_keys(base)
    assert _shown(manifest, today) == {i: [k] for i, k in keys.items() if i not in hollow}


def test_the_views_documents_are_what_housec_writes_for_them(cottage):
    manifest, out, _ = cottage
    for vid in manifest["views"]:
        assert (out / "options" / f"{vid}.json").read_text() == housec.document_text(
            housec.compile_house(HOUSE, vid)), vid


@COTTAGE_ONLY
def test_the_cottages_areas_views_and_plans(cottage):
    manifest, out, lines = cottage
    assert manifest["areas"] == [{"id": "kitchen", "name": manifest["areas"][0]["name"],
                                  "tags": ["kitchen/today", "kitchen/island", "kitchen/peninsula"],
                                  "today": "kitchen/today"}]
    assert manifest["views"] == {
        "kitchen_island": {"name": "The kitchen with a freestanding island and no uppers", "tags": ["kitchen/island"]},
        "kitchen_peninsula": {"name": "The kitchen with a peninsula off the west wall, today's uppers kept",
                              "tags": ["kitchen/peninsula"]}}
    assert manifest["unavailable"] == [] and manifest["location"]["latitude"] == 40.0
    assert set(manifest["plans"]) == {"today", "kitchen-island", "kitchen-peninsula"}
    assert "3 combinations in 1 group (3 kitchen)" in lines[0]


@COTTAGE_ONLY
def test_the_cottages_shell_and_switched_pieces(cottage):
    manifest, _, _ = cottage
    base = json.loads((ROOT / "house.json").read_text())
    switched = {"kitchen.base.back", "kitchen.counter.back", "kitchen.fridge", "kitchen.upper.left",
                "kitchen.upper.right"}
    shell = [p["id"] for p in manifest["pieces"] if p["chains"] == [[]]]
    # everything else of today's is the shell, but the porch's space, which draws nothing and so is no piece
    assert shell == [e["id"] for e in base["elements"] if e["id"] not in switched | {"porch.space"}]
    assert not [p for p in manifest["pieces"] if p["id"] == "porch.space"]
    # both alternatives carry the back run on to the refrigerator, moved: today's under kitchen/today, and the
    # alternatives' one piece, drawn once and shown under both their tags
    for eid in ("kitchen.base.back", "kitchen.counter.back", "kitchen.fridge"):
        assert _chains(manifest, eid, today=True) == [["kitchen/today"]]
        (alt,) = [p for p in manifest["pieces"] if p["id"] == eid and p["plan"] != "today"]
        assert alt["plan"] == "kitchen-island" and alt["chains"] == [["kitchen/island"], ["kitchen/peninsula"]]
    # the island takes the uppers down and the peninsula keeps today's
    assert _chains(manifest, "kitchen.upper.left") == [["kitchen/peninsula"], ["kitchen/today"]]
    assert [p["chains"] for p in manifest["pieces"] if p["id"] == "kitchen.upper.left"] == [
        [["kitchen/today"], ["kitchen/peninsula"]]]                   # today's chain first
    assert _chains(manifest, "kitchen.island.counter") == [["kitchen/island"]]
    assert _chains(manifest, "kitchen.peninsula.counter") == [["kitchen/peninsula"]]
    # pieces come in document order, today's first, then what the alternatives add
    order = [p["id"] for p in manifest["pieces"]]
    assert order.index("roof.plane.back") < order.index("kitchen.island.base")


# ------------------------------------------------------------------ the two-area design

@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fixture")
    manifest, house = _compile(FIXTURE / "house.star", tmp / "out", tmp / "house.json")
    return manifest, house, tmp / "out"


def test_every_combination_of_the_fixture_shows_exactly_what_it_composes_to(fixture):
    manifest, house, _ = fixture
    assert _holds(FIXTURE / "house.star", house, manifest) == 3 * 2


def test_the_fixtures_chains(fixture):
    manifest, _, _ = fixture
    # x removes box one, and y keeps it
    assert _chains(manifest, "room.box.one") == [["a/today"], ["a/y"]]
    # x moves box two: today's shows under today's a and y, x's own under x
    assert _chains(manifest, "room.box.two", today=True) == [["a/today"], ["a/y"]]
    assert _chains(manifest, "room.box.two", today=False) == [["a/x"]]
    assert _chains(manifest, "room.box.three") == [[]]
    # the box y adds is the very box x adds: one piece, drawn from x's plan, shown under y too; p takes it out under x
    (new,) = [p for p in manifest["pieces"] if p["id"] == "a.box.new"]
    assert new["plan"] == "a-x" and new["chains"] == [["a/x", "b/today"], ["a/y"]]
    # p takes box four out but cannot stand in today's a, where b's tag p shows today's b, box four with it
    assert _chains(manifest, "room.box.four") == [["a/today", "b/p"], ["b/today"]]
    # p stands where a's alternative says: a piece under each
    assert sorted(p["chains"] for p in manifest["pieces"] if p["id"] == "b.box.new") == [[["a/x", "b/p"]],
                                                                                         [["a/y", "b/p"]]]
    assert manifest["unavailable"] == [{"area": "b", "alt": "p", "combination": {"a": "today"},
                                        "code": "choice.no_a", "words": "p stands in x or y"}]
    assert manifest["views"]["ax_bp"]["tags"] == ["a/x", "b/p"] and manifest["views"]["ay"]["tags"] == ["a/y", "b/today"]


def test_a_view_of_the_fixture_composes_its_layers(fixture):
    _, _, out = fixture
    opt = json.loads((out / "options" / "ax_bp.json").read_text())["meta"]["option"]
    assert opt["layers"] == [{"area": "a", "alt": "x"}, {"area": "b", "alt": "p"}]
    assert (opt["removes"], opt["replaces"], opt["adds"]) == (["room.box.one", "room.box.four"], ["room.box.two"],
                                                              ["b.box.new"])
    # a later layer's check takes an earlier one's place by its code, and its drops take out what no longer holds
    assert opt["checks"] == ["width 12.0 - - ok"] and opt["later"] == []


# ------------------------------------------------------------------ the groups that stand on their own

C_TODAY_ONLY = ('load("zones/areas/box.star", "fixture_box")\n'
                '{"LAYER": {"id": "m", "name": "m", "removes": [], "replaces": [fixture_box("room.box.three", 45.0)], '
                '"adds": []}}\n')
C_COUPLED = C_TODAY_ONLY.replace('"room.box.three", 45.0', '"room.box.two", 27.0')


@pytest.mark.parametrize("layer, groups", [
    (C_TODAY_ONLY, [["a", "b"], ["c"]]),           # c touches nothing a or b does: composed apart
    (C_COUPLED, [["a", "b", "c"]]),                # c moves the box a's x moves: composed together
])
def test_areas_are_composed_apart_only_where_they_reach_no_common_element(tmp_path, layer, groups):
    shutil.copytree(FIXTURE, tmp_path / "house", ignore=shutil.ignore_patterns("house.json"))
    areas = tmp_path / "house" / "zones" / "areas.star"
    areas.write_text(areas.read_text().replace('"reads": ["a"]}]', '"reads": ["a"]}, {"id": "c", "name": "third"}]'))
    (tmp_path / "house" / "zones" / "areas" / "c").mkdir()
    (tmp_path / "house" / "zones" / "areas" / "c" / "m.star").write_text(layer)
    entry = tmp_path / "house" / "house.star"
    lines = []
    house = entry.parent / "house.json"
    house.write_text(housec.document_text(housec.compile_house(entry)))
    manifest = areasc.compile_areas(str(entry), str(house), str(tmp_path / "out"), echo=lines.append)
    assert f"in {len(groups)} group" in lines[0]
    design = housec.Design(entry)
    made = areasc.independent_groups(design, lambda areas: {"areas": areas, "touched": _touched(design, house, areas)})
    assert [g["areas"] for g in made] == groups
    assert _holds(entry, house, manifest) == 3 * 2 * 2


def _touched(design, house, areas):
    raw = house.read_bytes()
    base = json.loads(raw)
    keys, hollow, plan = areasc.planned_keys(base)
    today = {"name": "today", "doc": base, "keys": keys, "hollow": hollow, "plan": plan,
             "index": {e["id"]: i for i, e in enumerate(base["elements"])}}
    tags = {housec.layer_tag(a, alt) for a in design.alts for alt in ["today"] + design.alts[a]}
    return areasc._compose_group(areas, design, base, hashlib.sha256(raw).hexdigest(), tags, today)["touched"]


def test_an_alternative_that_can_stand_nowhere_is_an_error(tmp_path, monkeypatch):
    real = housec.Design
    monkeypatch.setattr(housec, "Design", lambda e, rewrite=None: real(
        e, lambda src: src.replace("if at == None:", "if True:")))
    with pytest.raises(areasc.AreasError, match="b/p can stand in no combination at all"):
        _compile(FIXTURE / "house.star", tmp_path / "out", tmp_path / "house.json")


def test_a_proof_composes_every_combination_until_there_are_too_many():
    design = housec.Design(FIXTURE / "house.star")
    every = design.combinations()
    assert areasc.proof_combinations(design, [["a", "b"]]) == every and len(every) == 6
    few = areasc.proof_combinations(design, [["a"], ["b"]], limit=4)
    own = [c for c in every if c["a"] == "today" or c["b"] == "today"]
    assert few[:len(own)] == own and len(few) == len(own) == 4


# ------------------------------------------------------------------ content keys and the out directory

@COTTAGE_ONLY
def test_a_key_covers_an_elements_planned_record_and_nothing_else():
    doc = json.loads((ROOT / "house.json").read_text())
    base = areasc.keys(doc)
    moved = copy.deepcopy(doc)
    fridge = next(e for e in moved["elements"] if e["id"] == "kitchen.fridge")
    fridge["bbox"][0] += 1.0
    assert {i for i, k in areasc.keys(moved).items() if k != base[i]} == {"kitchen.fridge"}
    tagged = copy.deepcopy(doc)
    tagged["elements"][0]["tags"] = ["kitchen/island"]            # which alternative gave it draws nothing
    assert areasc.keys(tagged, {"kitchen/island"}) == base and areasc.keys(tagged) != base
    defaulted = copy.deepcopy(doc)
    defaulted["elements"][0]["build_floor"] = True                 # what the planner fills in anyway
    assert areasc.keys(defaulted) == base
    refused = copy.deepcopy(doc)
    fridge = next(e for e in refused["elements"] if e["id"] == "kitchen.fridge")
    fridge["bbox"] = [0.0, 0.0, 0.0, 0.0, 10.0, 10.0]
    with pytest.raises(areasc.AreasError, match="does not plan"):
        areasc.keys(refused)


def test_a_house_json_housec_did_not_write_is_refused(tmp_path):
    other = tmp_path / "house.json"
    other.write_text(json.dumps(json.loads((ROOT / "house.json").read_text())))
    with pytest.raises(areasc.AreasError, match="not a document housec wrote"):
        areasc.compile_areas(str(HOUSE), str(other), str(tmp_path / "out"), echo=lambda s: None)


def test_plans_the_manifest_no_longer_names_are_removed(tmp_path):
    stale = tmp_path / "out" / "areas" / "plans" / "kitchen-gone.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    manifest = areasc.compile_areas(str(HOUSE), str(ROOT / "house.json"), str(tmp_path / "out"), echo=lambda s: None)
    assert not stale.exists()
    assert sorted(p.name for p in stale.parent.iterdir()) == sorted(Path(p).name for p in manifest["plans"].values())


def test_a_house_without_areas_is_all_shell_with_one_plan(tmp_path):
    (tmp_path / "house").mkdir()
    shutil.copy(FIXTURE / "house.star", tmp_path / "house" / "house.star")
    manifest, _ = _compile(tmp_path / "house" / "house.star", tmp_path / "out")
    assert manifest["areas"] == [] and manifest["views"] == {} and list(manifest["plans"]) == ["today"]
    assert [p["chains"] for p in manifest["pieces"]] == [[[]]] * 4
