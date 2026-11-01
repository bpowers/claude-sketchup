"""look.py's steps without SketchUp: run against a tiny stand-in project (its own housec.py, ir/plan.py, areasc.py and
overlay/restamp.py, each a few lines that write what the real one writes) and a stand-in bridge. A failing step stops
the look; --compile-only never touches the bridge; the owner's SketchUp state is read around the sync and any change
to it is named; the lock serialises looks and names its holder."""
from __future__ import annotations

import fcntl
import json
import textwrap
from pathlib import Path

import pytest

import look

HOUSEC = """
import json, sys
src = open(sys.argv[1]).read()
if "FAIL" in src:
    sys.exit("house.star: a deliberate failure")
open(sys.argv[3], "w").write(json.dumps({"ir_version": 1, "elements": [], "src": src}))
"""
PLAN = """
import sys
doc = open(sys.argv[1]).read()
if "REFUSE" in doc:
    print("refused living.wall.front: a deliberate refusal")
    sys.exit(1)
print("warning living.space: a deliberate warning")
"""
AREASC = """
import json, os
os.makedirs("out/areas", exist_ok=True)
json.dump({"manifest": 1, "areas": [{"id": "kitchen", "name": "Kitchen",
           "tags": ["kitchen/today", "kitchen/island", "kitchen/peninsula"], "today": "kitchen/today"}],
           "pieces": []}, open("out/areas/manifest.json", "w"))
"""
RESTAMP = """
import sys
open("restamped.txt", "w").write(" ".join(sys.argv[1:]))
"""


@pytest.fixture
def project(tmp_path):
    """A stand-in project root with the four scripts look.py runs."""
    (tmp_path / "ir").mkdir()
    (tmp_path / "overlay").mkdir()
    for rel, text in (("housec.py", HOUSEC), ("ir/__init__.py", ""), ("ir/plan.py", PLAN), ("areasc.py", AREASC),
                      ("overlay/__init__.py", ""), ("overlay/restamp.py", RESTAMP)):
        (tmp_path / rel).write_text(textwrap.dedent(text))
    (tmp_path / "house.star").write_text("# a house\n")
    return tmp_path


def _state(tags=(("Layer0", True), ("kitchen/today", True)), tops=(("HOUSE", 1, False, False),), active="Layer0",
           folders=(), on=None):
    """look.STATE's answer: `tops` as (name, entity, hidden, locked), on Layer0 unless `on` maps a name to a tag."""
    return {"tags": [{"name": n, "visible": v, "folder": None} for n, v in tags],
            "folders": [{"name": n, "visible": v, "folder": None} for n, v in folders],
            "tops": [{"entity": e, "kind": "Group", "name": n, "tag": (on or {}).get(n, "Layer0"), "hidden": h,
                      "locked": lk} for n, e, h, lk in tops],
            "active_tag": active}


class Bridge:
    """SketchUp stood in: eval answers the owner's state (before, then after), sync records its call."""

    def __init__(self, before=None, after=None, sync_error=None, state_error=None):
        self.states = [before or _state(), after or before or _state()]
        self.sync_error, self.state_error = sync_error, state_error
        self.calls = []

    def eval(self, code):
        assert "Sketchup.active_model" in code
        self.calls.append("eval")
        if self.state_error:
            raise self.state_error
        return self.states.pop(0)

    def sync(self, path, rebuild=False):
        self.calls.append(("sync", Path(path).name, rebuild))
        if self.sync_error:
            raise self.sync_error
        return {"built": 3, "kept": 40, "erased": 1}


class Never:
    def __getattr__(self, name):
        raise AssertionError("the bridge may not be used")


def test_compile_only_runs_the_three_compile_steps_and_never_the_bridge(project, capfd):
    assert look.main(["--compile-only"], root=project, bridge=Never()) == 0
    out = capfd.readouterr().out
    assert json.loads((project / "house.json").read_text())["src"] == "# a house\n"
    assert (project / "out" / "areas" / "manifest.json").is_file()
    assert "a deliberate warning" in out
    assert [ln.split(":")[0] for ln in out.splitlines() if ln.startswith("== ") and ":" in ln][:3] == \
        ["== compile", "== plan", "== areas"]
    last = out.strip().splitlines()[-1]
    assert last.startswith("== house ") and "compile" in last and "plan" in last and "areas" in last


def test_a_failing_compile_stops_the_look(project, capfd):
    (project / "house.star").write_text("FAIL\n")
    assert look.main([], root=project, bridge=Never()) == 1
    out = capfd.readouterr().out
    assert "== compile: exit 1, stopping" in out and "== plan" not in out
    assert not (project / "out" / "areas" / "manifest.json").exists()


def test_a_refusal_stops_the_look_before_the_areas(project, capfd):
    (project / "house.star").write_text("REFUSE\n")
    assert look.main(["--compile-only"], root=project, bridge=Never()) == 1
    out = capfd.readouterr().out
    assert "a deliberate refusal" in out and "== plan: exit 1, stopping" in out and "== areas" not in out


def test_a_look_syncs_reads_the_owners_state_around_it_and_restamps_the_photos(project, capfd):
    br = Bridge()
    assert look.main(["living-01", "kitchen-02", "--rebuild"], root=project, bridge=br) == 0
    out = capfd.readouterr().out
    assert br.calls == ["eval", ("sync", "manifest.json", True), "eval"]
    assert (project / "restamped.txt").read_text() == "living-01 kitchen-02"
    assert "== owner state: unchanged" in out and "built 3, kept 40, erased 1" in out
    assert "out/overlays/living-01_overlay.png" in out


def test_a_sync_that_changes_the_owners_state_is_named_and_fails_the_look(project, capfd):
    before = _state(tops=(("HOUSE", 1, False, False), ("FURNISHINGS", 7, False, False)))
    after = _state(tags=(("Layer0", True), ("kitchen/today", False), ("kitchen/island", True)),
                   tops=(("HOUSE", 2, False, False),))
    assert look.main([], root=project, bridge=Bridge(before, after)) == 1
    out = capfd.readouterr().out
    assert "tag kitchen/today was shown, now hidden" in out
    assert "the new tag kitchen/island came up shown" in out
    assert "Group FURNISHINGS (7) is gone" in out
    assert "HOUSE" not in "".join(ln for ln in out.splitlines() if "WARNING" in ln)   # HOUSE is the sync's own


def test_a_failed_sync_names_the_element_and_stops_before_the_photos(project, capfd):
    class BridgeError(RuntimeError):
        element_id = "living.wall.front"

    assert look.main(["living-01"], root=project, bridge=Bridge(sync_error=BridgeError("a face would not close"))) == 1
    out = capfd.readouterr().out
    assert "== sync: failed at living.wall.front: a face would not close" in out
    assert not (project / "restamped.txt").exists()


def test_a_sketchup_that_does_not_answer_stops_the_look_without_a_sync(project, capfd):
    class BridgeTimeout(RuntimeError):
        pass

    br = Bridge(state_error=BridgeTimeout("nothing picked up the request in 120 s"))
    assert look.main([], root=project, bridge=br) == 1
    assert br.calls == ["eval"]
    assert "SketchUp did not answer" in capfd.readouterr().out


def test_a_state_that_cannot_be_read_is_a_warning_not_a_silence(project, capfd):
    br = Bridge(state_error=RuntimeError("undefined method 'folders'"))
    assert look.main([], root=project, bridge=br) == 1
    assert "the owner's state was not read (undefined method 'folders')" in capfd.readouterr().out


def test_owner_changes_follows_tags_folders_the_active_tag_and_top_groups():
    base = _state(tops=(("HOUSE", 1, True, True), ("FURNISHINGS", 7, False, False), ("", 9, False, False)),
                  folders=(("furnishings", True),))
    assert look.owner_changes(base, base) == []
    # a rebuilt HOUSE is a new entity of the same name: followed by name, keeping hidden and locked
    same = _state(tops=(("HOUSE", 5, True, True), ("FURNISHINGS", 7, False, False), ("", 9, False, False)),
                  folders=(("furnishings", True),))
    assert look.owner_changes(base, same) == []
    changed = _state(tags=(("Layer0", True), ("kitchen/today", True), ("kitchen/today_2", True)),
                     tops=(("HOUSE", 5, False, False), ("FURNISHINGS", 7, True, False), ("HOUSE", 6, False, False)),
                     active="kitchen/today", folders=(("furnishings", False),))
    said = look.owner_changes(base, changed, created_hidden={"kitchen/island"})
    assert "HOUSE: 2 top-level groups of that name after the sync, from 1" in said
    assert "Group FURNISHINGS (7) was shown, now hidden" in said
    assert "Group (unnamed) (9) is gone" in said
    assert "tag folder furnishings was shown, now hidden" in said
    assert "the active tag was Layer0, now kitchen/today" in said
    assert not [s for s in said if "today_2" in s]      # a new tag that is no alternative may show
    unlocked = look.owner_changes(base, _state(tops=(("HOUSE", 5, True, False), ("FURNISHINGS", 7, False, False),
                                                     ("", 9, False, False)), folders=(("furnishings", True),)))
    assert unlocked == ["HOUSE was locked, now unlocked"]
    tops = (("HOUSE", 1, True, True), ("FURNISHINGS", 7, False, False), ("", 9, False, False))
    retagged = look.owner_changes(_state(tops=tops, folders=(("furnishings", True),), on={"HOUSE": "model"}),
                                  _state(tops=tops, folders=(("furnishings", True),), on={"FURNISHINGS": "model"}))
    assert retagged == ["HOUSE was on tag model, now on Layer0", "Group FURNISHINGS (7) was on tag Layer0, now on model"]


def test_hidden_when_new_is_every_alternative_but_today():
    man = {"areas": [{"id": "kitchen", "tags": ["kitchen/today", "kitchen/island"], "today": "kitchen/today"},
                     {"id": "bath", "tags": ["bath/today", "bath/tub"], "today": "bath/today"}]}
    assert look.hidden_when_new(man) == {"kitchen/island", "bath/tub"}
    assert look.hidden_when_new(None) == set()


def test_the_lock_names_its_holder_and_clears_it(tmp_path):
    path = tmp_path / "out" / "look.lock"
    with look.lock("probe", path):
        assert path.read_text().startswith("probe (pid")
        with open(path) as fh:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert path.read_text() == ""
    with open(path) as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)      # free again
