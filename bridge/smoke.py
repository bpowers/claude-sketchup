#!/usr/bin/env python3
"""A live check of the bridge and the builder in the open SketchUp, in a few seconds.

    uv run python bridge/smoke.py                   # every fixture in build/fixtures
    uv run python bridge/smoke.py --only walls roof
    uv run python bridge/smoke.py --keep            # leave the last fixture's group, hidden, to look at

It refuses to start unless the bridge answers ping. Then it builds each fixture document into the throwaway top group
SMOKE (never HOUSE), hiding it at once, and checks the build's stats: every element drawn, every solid's volume check
passed (a failed one stops the build with the element's id), the faces there. It checks that refused.json is refused
before anything is sent, renders one edges picture of sample.json to out/smoke/sample_edges.png, then erases SMOKE
and the tags and tag folders its builds made that the model did not have before. It prints what it did and how long
each step took, and exits 1 if anything failed.

SketchUp is the owner's window: the builds and the render leave HOUSE, FURNISHINGS, the camera and the owner's tags as
they were (the render puts back what it changes), but each build and the clean-up is a step on the owner's undo stack,
so ask before running it while they are working.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:               # run as a script (uv run python bridge/smoke.py)
    sys.path.insert(0, str(ROOT))

from bridge.client import Bridge, BridgeError, Refused, sha256_file  # noqa: E402
from ir import geom, plan as irplan  # noqa: E402

FIXTURES = ROOT / "build" / "fixtures"
TOP = "SMOKE"
PICTURE = "sample"                         # the fixture whose build is rendered
OUT = ROOT / "out" / "smoke"


def on_top(action: str) -> str:
    """Ruby that does `action` ("hidden = true") to every SMOKE group in one undo step; returns how many."""
    return (f"m = Sketchup.active_model; g = m.entities.grep(Sketchup::Group).select {{ |x| x.valid? && x.name == "
            f"'{TOP}' }}; m.start_operation('House twin: smoke check', true); g.each {{ |x| x.{action} }}; "
            "m.commit_operation; g.length")


# The model's tag and tag folder names, read before the first build so the clean-up knows which are the smoke's.
NAMES = ("(lambda do m = Sketchup.active_model; { 'tags' => m.layers.map(&:name), 'folders' => "
         "(m.layers.respond_to?(:folders) ? m.layers.folders.map(&:name) : []) } end).call")


def ruby_string(text: str) -> str:
    """text as a Ruby single-quoted literal."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def clean_up(before: dict) -> str:
    """Ruby that, in one undo step, erases every SMOKE group and removes the tags and top-level tag folders the model
    did not have in `before` (NAMES): the builds made them for the fixtures' zones, and nothing else uses them. Run in
    a lambda, so it leaves no local variable in the watcher's shared eval binding."""
    return f"""(lambda do
  m = Sketchup.active_model
  had = JSON.parse({ruby_string(json.dumps(before))})
  m.start_operation('House twin: smoke check', true)
  smoke = m.entities.grep(Sketchup::Group).select {{ |x| x.valid? && x.name == '{TOP}' }}
  smoke.each(&:erase!)
  tags = []
  if m.layers.respond_to?(:remove)
    tags = m.layers.to_a.drop(1).map(&:name).reject {{ |n| had['tags'].include?(n) }}
    tags.each {{ |n| m.layers.remove(m.layers[n], false) }}
  end
  folders = []
  if m.layers.respond_to?(:folders) && m.layers.respond_to?(:remove_folder)
    made = m.layers.folders.reject {{ |f| had['folders'].include?(f.name) }}
    made = made.select {{ |f| f.count_layers.zero? && f.count_folders.zero? }}
    folders = made.map(&:name)
    made.each {{ |f| m.layers.remove_folder(f) }}
  end
  m.commit_operation
  {{ 'erased' => smoke.length, 'tags' => tags, 'folders' => folders }}
end).call"""


def extent(elements):
    """(lo [x, y, z], hi [x, y, z]) of planned elements."""
    pts = []
    for e in elements:
        for k in ("outline", "pts"):
            pts += [p[:2] for p in e.get(k) or []]
        for k in ("a", "b", "center"):
            if e.get(k):
                pts.append(e[k])
        if e.get("bbox"):
            pts += [e["bbox"][:2], e["bbox"][3:5]]
    zs = [z for e in elements for z in geom.element_zrange(e)]
    lo = [min(p[0] for p in pts), min(p[1] for p in pts), min(zs)]
    hi = [max(p[0] for p in pts), max(p[1] for p in pts), max(zs)]
    return lo, hi


def picture_request(elements, out: Path) -> dict:
    """An edges view from the low x, low y corner, above, that holds every element."""
    lo, hi = extent(elements)
    c = [(a + b) / 2 for a, b in zip(lo, hi)]
    r = math.dist(lo, hi) / 2
    fov = 40.0
    d = r / math.sin(math.radians(fov / 2)) * 1.05
    direction = [-0.6, -0.65, 0.47]
    n = math.sqrt(sum(v * v for v in direction))
    eye = [c[k] + direction[k] / n * d for k in range(3)]
    return {"out": str(out), "width": 1200, "height": 800, "eye": eye, "target": c, "up": [0, 0, 1], "fov_v_deg": fov,
            "style": "edges", "transparent": False, "top": TOP}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="+", help="fixture names (without .json)")
    ap.add_argument("--keep", action="store_true", help="leave the last fixture's group, hidden")
    a = ap.parse_args(argv)
    t_all = time.monotonic()
    names = a.only or sorted(p.stem for p in FIXTURES.glob("*.json"))
    failures = []

    def line(ok, what, t0):
        print(f"{'ok  ' if ok else 'FAIL'} {time.monotonic() - t0:6.2f} s  {what}")
        if not ok:
            failures.append(what)

    t0 = time.monotonic()
    try:
        info = Bridge(max_pickup=20).ping()
    except BridgeError as e:
        print(f"the bridge does not answer, so nothing was sent: {e}\n(bridge/README.md says how to start the watcher)")
        return 1
    line(True, f"ping: SketchUp {info.get('sketchup')}, model {info.get('model')!r}, project {info.get('project')}", t0)
    if info.get("project") and Path(info["project"]).resolve() != ROOT:
        print(f"note: the watcher serves {info['project']}, not this project; builds read plans by absolute path, "
              "but its build.rb is that project's")

    br = Bridge(timeout=600)
    before = br.eval(NAMES)
    last = None
    try:
        for name in names:
            path = FIXTURES / f"{name}.json"
            t0 = time.monotonic()
            plan = irplan.plan_document(json.loads(path.read_text()), str(path))
            if not plan.ok:
                try:
                    br.build(path, top=TOP)
                    line(False, f"{name}: does not plan, yet build() sent it", t0)
                except Refused as e:
                    line(True, f"{name}: refused before sending ({len(e.element_ids)} elements)", t0)
                continue
            drawn = [e for e in plan.elements if not (e["type"] == "space" and not e["build_floor"]
                                                      and not e["build_ceiling"])]
            try:
                stats = br.build(path, top=TOP)
                br.eval(on_top("hidden = true"))
            except BridgeError as e:
                line(False, f"{name}: {e}", t0)
                continue
            problems = []
            if stats.get("top") != TOP or stats.get("elements") != len(plan.elements):
                problems.append(f"built {stats.get('elements')} elements into {stats.get('top')}")
            if stats.get("built") != len(drawn):
                problems.append(f"drew {stats.get('built')} groups for {len(drawn)} elements that draw something")
            if not stats.get("volume_checks") or not stats.get("faces"):
                problems.append(f"{stats.get('volume_checks')} volume checks, {stats.get('faces')} faces")
            line(not problems, f"{name}: {stats.get('built')} elements, {stats.get('volume_checks')} volume checks, "
                               f"{stats.get('faces')} faces, {stats.get('intersection_edges')} crease edges"
                               + (f"; {'; '.join(problems)}" if problems else ""), t0)
            last = (name, path, plan)
            if name == PICTURE:
                t0 = time.monotonic()
                try:
                    res = br.render(picture_request(plan.elements, OUT / f"{name}_edges.png"),
                                    expect_sha256=sha256_file(path))
                    ok = Path(res["out"]).is_file() and (res["width"], res["height"]) == (1200, 800)
                    line(ok, f"render: {res['out']} ({res['width']}x{res['height']}, {res['seconds']} s in SketchUp)",
                         t0)
                except BridgeError as e:
                    line(False, f"render: {e}", t0)
    finally:
        t0 = time.monotonic()
        if a.keep and last:
            line(True, f"kept {TOP} (hidden) with {last[0]} in it", t0)
        else:
            try:
                done = br.eval(clean_up(before))
                line(True, f"erased {done['erased']} {TOP} group{'' if done['erased'] == 1 else 's'}, "
                           f"{len(done['tags'])} tags and {len(done['folders'])} tag folders the builds made", t0)
            except BridgeError as e:
                line(False, f"erasing {TOP}: {e}", t0)
    print(f"{'all ok' if not failures else f'{len(failures)} failed'} in {time.monotonic() - t_all:.1f} s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
