"""bridge/client.py against a fake watcher: a thread that answers the trigger file the way bridge/watcher.rb does.

Nothing here talks to SketchUp. The fake watches a temporary project's bridge/trigger, writes the "running" reply and
then the final one, and records every request it saw, so a test fails if a refused document would have been sent.

    uv run pytest tests/test_client.py
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import pytest

from bridge import client
from bridge.client import Bridge, BridgeError, BridgeTimeout, Refused

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "build" / "fixtures"
ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


class FakeWatcher:
    """Polls <root>/bridge/trigger like watcher.rb: on a new mtime it reads the request, writes {"state": "running"},
    then asks `answer(request)` for the final reply's fields ({"result": ...} or {"error": {...}}; None never
    answers) and writes it. `seen` records every request, `overlap` whether two were ever in hand at once."""

    def __init__(self, root: Path, answer):
        self.root, self.answer = root, answer
        self.trigger = root / "bridge" / "trigger"
        self.replies = root / "out" / "bridge"
        self.seen, self.busy, self.overlap = [], 0, False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def _write(self, req, payload):
        self.replies.mkdir(parents=True, exist_ok=True)
        path = self.replies / f"{req['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": req["id"], "cmd": req["cmd"], "protocol": 1, **payload}))
        tmp.replace(path)

    def _run(self):
        last = None
        while not self._stop.is_set():
            try:
                stamp = self.trigger.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if stamp is not None and stamp != last:
                last = stamp
                req = json.loads(self.trigger.read_text())
                self.busy += 1
                self.overlap |= self.busy > 1
                self.seen.append(req)
                self._write(req, {"state": "running", "message": f"{req['cmd']} running"})
                fields = self.answer(req)
                if fields is not None:
                    state = "error" if "error" in fields else "ok"
                    self._write(req, {"state": state, "message": f"{req['cmd']} {state}", **fields})
                self.busy -= 1
            time.sleep(0.005)


def ok(result):
    return lambda req: {"result": result}


def bridge(root, **kw):
    return Bridge(root, activate=False, poll=0.005, **kw)


def test_a_request_round_trip(tmp_path):
    with FakeWatcher(tmp_path, ok({"protocol": 1, "sketchup": "26.0"})) as w:
        assert bridge(tmp_path).ping() == {"protocol": 1, "sketchup": "26.0"}
    (req,) = w.seen
    assert req["bridge"] == client.PROTOCOL and req["cmd"] == "ping" and ID_RE.fullmatch(req["id"])
    assert not (tmp_path / "out" / "bridge" / f"{req['id']}.json").exists()          # the reply is taken away


def test_eval_sends_the_code(tmp_path):
    with FakeWatcher(tmp_path, lambda req: {"result": 2, "eval": "2"}) as w:
        assert bridge(tmp_path).eval("1 + 1") == 2
    assert w.seen[0]["code"] == "1 + 1"


def test_an_error_reply_raises_with_the_ruby_details(tmp_path):
    err = {"class": "HouseTwin::BuildError", "message": "living.wall.south: the wall body has volume 1",
           "backtrace": ["build.rb:10:in `check_volume'"], "element_id": "living.wall.south"}
    with FakeWatcher(tmp_path, lambda req: {"error": err}):
        with pytest.raises(BridgeError) as info:
            bridge(tmp_path).request("sync", path="/x")
    e = info.value
    assert (e.ruby_class, e.element_id) == ("HouseTwin::BuildError", "living.wall.south")
    assert "check_volume" in str(e) and e.backtrace == err["backtrace"]


def test_build_plans_first_and_sends_the_plan(tmp_path):
    path = FIXTURES / "sample.json"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    def answer(req):
        plan = json.loads(Path(req["path"]).read_text())
        return {"result": {"ir_sha256": plan["ir_sha256"], "elements": len(plan["elements"]), "top": req["top"]}}
    with FakeWatcher(tmp_path, answer) as w:
        out = bridge(tmp_path).build(path, top="TEST sample")
    (req,) = w.seen
    plan = json.loads(Path(req["path"]).read_text())
    assert req["cmd"] == "build" and Path(req["path"]).parent == tmp_path / "out" / "plans"
    assert plan["plan"] == 1 and plan["ir"] == str(path) and plan["ir_sha256"] == sha
    assert out["top"] == "TEST sample" and out["elements"] == 12 and out["plan_warnings"] == []


def test_a_build_keeps_its_own_plan_however_many_newer_ones_there_are(tmp_path):
    """The plan of an unchanged document keeps its old time stamp; the prune of out/plans must still keep it."""
    path = FIXTURES / "sample.json"

    def answer(req):
        if not Path(req["path"]).is_file():
            return {"error": {"class": "HouseTwin::BuildError", "message": f"no plan at {req['path']}"}}
        plan = json.loads(Path(req["path"]).read_text())
        return {"result": {"ir_sha256": plan["ir_sha256"], "elements": len(plan["elements"]), "top": req["top"]}}
    with FakeWatcher(tmp_path, answer) as w:
        br = bridge(tmp_path)
        br.build(path, top="TEST sample")
        plans = tmp_path / "out" / "plans"
        for i in range(client.PLANS_KEPT + 4):
            (plans / f"newer{i:02d}.json").write_text("{}")
        br.build(path, top="TEST sample")
    assert len(w.seen) == 2 and len(list(plans.glob("*.json"))) == client.PLANS_KEPT
    assert Path(w.seen[1]["path"]).is_file()


def test_views_names_a_view_and_sends_its_tags(tmp_path):
    manifest = tmp_path / "out" / "areas" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"views": {"kitchen_island": {"name": "Island", "tags": ["kitchen/island"]}}}))
    with FakeWatcher(tmp_path, ok(["out/views/x/iso.png"])) as w:
        br = bridge(tmp_path)
        br.views()
        br.views("kitchen_island")
        with pytest.raises(BridgeError, match="no view 'galley'"):
            br.views("galley")
    assert [(r["name"], r["areas"]) for r in w.seen] == [("today", []), ("kitchen_island", ["kitchen/island"])]


def test_a_document_that_does_not_plan_is_never_sent(tmp_path):
    with FakeWatcher(tmp_path, ok({})) as w:
        with pytest.raises(Refused) as info:
            bridge(tmp_path).build(FIXTURES / "refused.json")
    assert w.seen == [] and not (tmp_path / "bridge" / "trigger").exists()
    e = info.value
    assert e.element_id == "Living Wall" and "refused.wall.5" in e.element_ids
    assert "refused.wall.5: opening refused.wall.5.w: head_z 100" in str(e)


def test_a_build_answered_for_another_document_is_an_error(tmp_path):
    with FakeWatcher(tmp_path, ok({"ir_sha256": "0" * 64})):
        with pytest.raises(BridgeError, match="not the stats of a build"):
            bridge(tmp_path).build(FIXTURES / "sample.json")


def test_sync_refuses_a_manifest_compiled_on_another_house(tmp_path):
    house = tmp_path / "house.json"
    house.write_text("{}")
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps({"manifest": 1, "house": {"path": str(house), "sha256": "0" * 64}}))
    with FakeWatcher(tmp_path, ok({})) as w:
        with pytest.raises(BridgeError, match="compile again"):
            bridge(tmp_path).sync(man)
        man.write_text(json.dumps({"manifest": 1, "house": {"path": str(house), "sha256": client.sha256_file(house)}}))
        bridge(tmp_path).sync(man, rebuild=True)
    assert [(r["cmd"], r["rebuild"]) for r in w.seen] == [("sync", True)]


def test_render_resolves_out_against_the_root_and_sends_the_request(tmp_path):
    req_file = tmp_path / "req.json"
    req_file.write_text(json.dumps({"out": "out/renders/x.png", "width": 8}))
    with FakeWatcher(tmp_path, lambda req: {"result": {"out": req["request"]["out"]}}) as w:
        out = bridge(tmp_path).render(req_file, expect_sha256="a" * 64)
        bridge(tmp_path).render({"out": "/abs/y.png"})
    assert out["out"] == str(tmp_path / "out" / "renders" / "x.png")
    assert w.seen[0]["expect_ir_sha256"] == "a" * 64 and w.seen[0]["request"]["width"] == 8
    assert w.seen[1]["request"] == {"out": "/abs/y.png"} and "expect_ir_sha256" not in w.seen[1]


def test_a_malformed_sha_is_refused_before_sending(tmp_path):
    with FakeWatcher(tmp_path, ok({})) as w:
        with pytest.raises(BridgeError, match="64 lowercase hex"):
            bridge(tmp_path).render({"out": "/x.png"}, expect_sha256="ABC")
    assert w.seen == []


def test_no_watcher_times_out_waiting_for_pickup(tmp_path):
    t0 = time.monotonic()
    with pytest.raises(BridgeTimeout, match="not picked up within 0.3 s"):
        bridge(tmp_path, max_pickup=0.3).ping()
    assert time.monotonic() - t0 < 5


def test_a_request_picked_up_and_never_finished_times_out(tmp_path):
    with FakeWatcher(tmp_path, lambda req: None):
        with pytest.raises(BridgeTimeout, match="did not finish within 0.4 s"):
            bridge(tmp_path, timeout=0.4).ping()


def test_requests_from_several_threads_are_serialised(tmp_path):
    def slow(req):
        time.sleep(0.05)
        return {"result": req["code"]}
    got = []
    with FakeWatcher(tmp_path, slow) as w:
        threads = [threading.Thread(target=lambda i=i: got.append(bridge(tmp_path).eval(str(i)))) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
    assert sorted(got) == ["0", "1", "2", "3"] and len(w.seen) == 4 and not w.overlap


def test_the_running_sketchup_is_found_in_ps_output():
    ps = ("/usr/sbin/cfprefsd agent\n"
          "/Applications/SketchUp 2026/SketchUp.app/Contents/MacOS/SketchUp -psn_0_123\n")
    assert client.running_app(ps) == "/Applications/SketchUp 2026/SketchUp.app"
    assert client.running_app("/usr/bin/login\n") is None
    assert client.bundle_id("/nonexistent/SketchUp.app") is None


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(client, "ROOT", tmp_path)        # Bridge() with no root serves the project at ROOT
    with FakeWatcher(tmp_path, ok({"protocol": 1})):
        assert client.main(["--no-activate", "ping"]) == 0
    assert json.loads(capsys.readouterr().out) == {"protocol": 1}
    assert client.main(["build", str(FIXTURES / "refused.json")]) == 1
    assert "refusing to build" in capsys.readouterr().err


def test_app_nap_brings_the_running_sketchup_to_the_front(tmp_path, monkeypatch):
    """On macOS a request nobody picks up within pickup_timeout brings SketchUp to the front, by the running app's own
    bundle id; with SketchUp not running at all the client says so instead of waiting out max_pickup."""
    import plistlib
    app = tmp_path / "Apps" / "SketchUp 2026" / "SketchUp.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.sketchup.SketchUp.2026"}))
    others = "".join(f"/usr/libexec/daemon{i}\n" for i in range(12))
    ran = []

    def fake_run(args, **kw):
        ran.append(args)
        out = others + (f"{app}/Contents/MacOS/SketchUp\n" if sketchup_running else "")
        return subprocess.CompletedProcess(args, 0, stdout=out if args[0] == "ps" else "", stderr="")
    import subprocess
    monkeypatch.setattr(client.subprocess, "run", fake_run)
    monkeypatch.setattr(client.sys, "platform", "darwin")
    br = Bridge(tmp_path, activate=True, poll=0.005, pickup_timeout=0.05, max_pickup=0.3)
    sketchup_running = True
    with pytest.raises(BridgeTimeout, match="not picked up within"):
        br.ping()
    assert ["osascript", "-e", 'tell application id "com.sketchup.SketchUp.2026" to activate'] in ran
    sketchup_running = False
    ran.clear()
    with pytest.raises(BridgeTimeout, match="SketchUp is not running"):
        br.ping()
    assert not any(a[0] == "osascript" for a in ran)
