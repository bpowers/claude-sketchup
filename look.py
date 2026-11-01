"""The loop: compile the house and its design areas, sync SketchUp to them, and re-match the named photos.

    uv run python look.py [photo stems...] [--compile-only] [--rebuild] [--force] [--who NAME]

The steps, each stopping the look when it fails:

1. compile: housec.py house.star -o house.json
2. plan: ir.plan house.json (refusals stop the look; warnings are printed)
3. areas: areasc.py house.star (each view's document, the plans and out/areas/manifest.json)
4. sync: SketchUp's HOUSE group made to hold what the manifest places (bridge/client.py Bridge.sync). Only what changed
   is built, so a look after a small edit takes seconds; --rebuild builds everything again.
5. photos: for each stem, its camera re-solved on the new house, rendered and composed into out/overlays/
   (overlay.restamp; a photo whose overlays already show this house is skipped, unless --force)

--compile-only stops after step 3 and needs no SketchUp. The look ends with one line of timings.

Around the sync the look reads what the owner sets in SketchUp: every tag's and tag folder's visibility, the active
tag, and every top-level group's tag and hidden and locked flags. A sync must keep all of it (an alternative's tag that is
new comes up hidden), so the look names anything that changed. The exit status is 1 when a step failed or the sync
changed the owner's state (or it could not be read).

One SketchUp serves every session, and a camera renders only against the house it was solved on, so a look holds an
exclusive lock (out/look.lock) from the compile to the last overlay; a second look waits for it, and says who holds it.
bridge/furnishings.py takes the same lock.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "out" / "look.lock"
MANIFEST = Path("out") / "areas" / "manifest.json"
HOUSE_TOPS = ("HOUSE",)          # the top groups a sync makes or edits; every other one it must leave alone
# What the look prints of build.rb's sync stats: what the sync did, not every counter it keeps (out/build_report.json).
SYNC_SAID = ("built", "instanced", "kept", "erased", "placements", "seconds")
# (step, arguments to python) for steps 1..3, run from the project root
COMPILE = (
    ("compile", ["housec.py", "house.star", "-o", "house.json"]),
    ("plan", ["-m", "ir.plan", "house.json"]),
    ("areas", ["areasc.py", "house.star"]),
)
# What the owner sets in SketchUp that a sync must keep, read in one bridge eval. It runs in a lambda so it leaves no
# local variable in the watcher's shared eval binding.
STATE = r"""
(lambda do
  m = Sketchup.active_model
  sub = lambda { |c| c.respond_to?(:folders) ? c.folders.flat_map { |f| [f] + sub.call(f) } : [] }
  within = ->(x) { x.respond_to?(:folder) && x.folder ? x.folder.name : nil }
  { 'tops' => m.entities.select { |e| e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance) }.map do |e|
      { 'entity' => e.entityID, 'kind' => e.typename, 'name' => e.name, 'tag' => e.layer.name,
        'hidden' => e.hidden?, 'locked' => e.locked? }
    end,
    'tags' => m.layers.map { |l| { 'name' => l.name, 'visible' => l.visible?, 'folder' => within.call(l) } },
    'folders' => sub.call(m.layers).map { |f| { 'name' => f.name, 'visible' => f.visible?, 'folder' => within.call(f) } },
    'active_tag' => m.active_layer.name }
end).call
"""


# ------------------------------------------------------------------ the lock

LOCK_BYTE = 1 << 20             # Windows: the byte locked, past the holder's name, so a waiter can still read the name


def _take(fh, wait):
    """Lock an open lock file exclusively: True once held, False when another process holds it and `wait` is false."""
    if os.name == "nt":                 # SketchUp also runs on Windows, where there is no flock
        import msvcrt
        while True:
            fh.seek(LOCK_BYTE)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not wait:
                    return False
                time.sleep(0.2)
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


def _release(fh):
    if os.name == "nt":
        import msvcrt
        fh.seek(LOCK_BYTE)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def lock(who, path=None):
    """The look lock, held for the block. A holder writes its name into the file, so a waiter can say whom it waits
    for, and clears it before letting go."""
    path = Path(path or LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    held = False
    try:
        if not _take(fh, wait=False):
            fh.seek(0)
            print(f"== waiting for the look lock, held by: {fh.read().strip() or 'unknown'}", flush=True)
            t0 = time.monotonic()
            _take(fh, wait=True)
            print(f"== lock acquired after {time.monotonic() - t0:.0f}s", flush=True)
        held = True
        fh.seek(0)
        fh.truncate()
        fh.write(f"{who} (pid {os.getpid()}) since {_dt.datetime.now():%H:%M:%S}\n")
        fh.flush()
        yield
    finally:
        if held:                        # a waiter interrupted never held it, so the holder's name stays
            fh.seek(0)
            fh.truncate()
            fh.flush()
            _release(fh)
        fh.close()


# ------------------------------------------------------------------ steps

def run_step(name, args, timings, root=ROOT):
    """One step as a python subprocess in the project root, timed; whether it succeeded."""
    print(f"== {name}: {' '.join(args)}", flush=True)
    t0 = time.monotonic()
    code = subprocess.run([sys.executable, *args], cwd=root).returncode
    timings.append((name, time.monotonic() - t0, code == 0))
    if code != 0:
        print(f"== {name}: exit {code}, stopping", flush=True)
    return code == 0


def compile_steps(timings, root=ROOT):
    """Steps 1..3: whether all of them succeeded (a failure stops at that step)."""
    return all(run_step(name, args, timings, root) for name, args in COMPILE)


def read_manifest(root=ROOT):
    try:
        return json.loads((Path(root) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None


def hidden_when_new(manifest):
    """The tags a build creates hidden: every area's alternatives but its today."""
    if not manifest:
        return set()
    return {t for a in manifest.get("areas", []) for t in a["tags"]} - {a["today"] for a in manifest.get("areas", [])}


def _timed_out(e):
    return type(e).__name__ == "BridgeTimeout"


def read_state(bridge):
    """The owner's SketchUp state (STATE): (state, None), or (None, the exception) when it cannot be read."""
    try:
        return bridge.eval(STATE), None
    except Exception as e:                       # noqa: BLE001 - any failure means "not read", and is reported
        return None, e


def _first_line(e):
    return (str(e).splitlines() or [type(e).__name__])[0]


def _shown(visible):
    return "shown" if visible else "hidden"


def owner_changes(before, after, rebuilt=HOUSE_TOPS, created_hidden=()):
    """What a sync changed of what the owner sets, in words, from STATE read before and after it.

    Tags and tag folders keep their visibility and folder, and the active tag stays. A tag that is new is fine, but
    one a build creates hidden (`created_hidden`: the areas' alternatives) must not show. The top groups the sync
    makes or edits (`rebuilt`, followed by name) keep their tag (the first one's), hidden flag (when every group of the
    name was hidden) and locked flag (when any was); every other top-level group and component is followed by its
    entity id and must stay, with its tag and hidden flag."""
    out = []
    tags0 = {t["name"]: t for t in before["tags"]}
    tags1 = {t["name"]: t for t in after["tags"]}
    for name, t in tags0.items():
        now = tags1.get(name)
        if now is None:
            out.append(f"tag {name} is gone")
            continue
        if now["visible"] != t["visible"]:
            out.append(f"tag {name} was {_shown(t['visible'])}, now {_shown(now['visible'])}")
        if now.get("folder") != t.get("folder"):
            out.append(f"tag {name} was in folder {t.get('folder')}, now in {now.get('folder')}")
    for name, t in tags1.items():
        if name not in tags0 and name in created_hidden and t["visible"]:
            out.append(f"the new tag {name} came up shown, where a build creates it hidden")
    folders1 = {f["name"]: f for f in after["folders"]}
    for f in before["folders"]:
        now = folders1.get(f["name"])
        if now is None:
            out.append(f"tag folder {f['name']} is gone")
        elif now["visible"] != f["visible"]:
            out.append(f"tag folder {f['name']} was {_shown(f['visible'])}, now {_shown(now['visible'])}")
    if after.get("active_tag") != before.get("active_tag"):
        out.append(f"the active tag was {before.get('active_tag')}, now {after.get('active_tag')}")
    rebuilt = set(rebuilt)
    for name in sorted(rebuilt):
        was = [t for t in before["tops"] if t["name"] == name]
        now = [t for t in after["tops"] if t["name"] == name]
        if not was:
            continue
        if len(now) != 1:
            out.append(f"{name}: {len(now)} top-level groups of that name after the sync, from {len(was)}")
            continue
        hidden, locked = all(t["hidden"] for t in was), any(t["locked"] for t in was)
        if now[0].get("tag") != was[0].get("tag"):
            out.append(f"{name} was on tag {was[0].get('tag')}, now on {now[0].get('tag')}")
        if now[0]["hidden"] != hidden:
            out.append(f"{name} was {_shown(not hidden)}, now {_shown(not now[0]['hidden'])}")
        if now[0]["locked"] != locked:
            out.append(f"{name} was {'locked' if locked else 'unlocked'}, now "
                       f"{'locked' if now[0]['locked'] else 'unlocked'}")
    others = {t["entity"]: t for t in after["tops"]}
    for t in before["tops"]:
        if t["name"] in rebuilt:
            continue
        what = f"{t['kind']} {t['name'] or '(unnamed)'} ({t['entity']})"
        now = others.get(t["entity"])
        if now is None:
            out.append(f"{what} is gone")
            continue
        if now.get("tag") != t.get("tag"):
            out.append(f"{what} was on tag {t.get('tag')}, now on {now.get('tag')}")
        if now["hidden"] != t["hidden"]:
            out.append(f"{what} was {_shown(not t['hidden'])}, now {_shown(not now['hidden'])}")
    return out


def sync_step(bridge, timings, root=ROOT, rebuild=False):
    """Step 4, with the owner's state read around it: (whether the sync went through, lines to end the look with,
    whether any of them needs attention)."""
    manifest = read_manifest(root)
    if manifest is None:
        print(f"== sync: {MANIFEST} cannot be read; step 3 (areasc.py) writes it", flush=True)
        return False, [], False
    print(f"== sync: {MANIFEST}{' (rebuild)' if rebuild else ''}", flush=True)
    t0 = time.monotonic()
    before, why0 = read_state(bridge)
    if before is None and _timed_out(why0):
        timings.append(("sync", time.monotonic() - t0, False))
        print(f"== sync: SketchUp did not answer ({_first_line(why0)}); is it open with the watcher running? "
              "(bridge/README.md)", flush=True)
        return False, [], False
    try:
        result = bridge.sync(str(Path(root) / MANIFEST), rebuild=rebuild)
    except Exception as e:                       # noqa: BLE001 - the bridge's errors are the owner's to read
        timings.append(("sync", time.monotonic() - t0, False))
        element = getattr(e, "element_id", None)
        print(f"== sync: failed{f' at {element}' if element else ''}: {e}", flush=True)
        if _timed_out(e):
            print("== is SketchUp open with the watcher running? (bridge/README.md)", flush=True)
        return False, [], False
    timings.append(("sync", time.monotonic() - t0, True))
    if isinstance(result, dict):
        said = ", ".join(f"{k} {result[k]}" for k in SYNC_SAID if k in result)
        if said:
            print(f"== sync: {said}", flush=True)
    after, why1 = read_state(bridge)
    if before is None or after is None:
        return True, [f"== WARNING the owner's state was not read ({_first_line(why0 or why1)}), so the look "
                      "cannot say it is kept"], True
    changes = owner_changes(before, after, HOUSE_TOPS, hidden_when_new(manifest))
    if changes:
        return True, [f"== WARNING the sync changed the owner's state: {c}" for c in changes], True
    return True, [f"== owner state: unchanged ({len(after['tags'])} tags, {len(after['folders'])} tag folders, the "
                  f"active tag, {len(after['tops'])} top-level groups and components)"], False


def photo_step(stems, timings, root=ROOT, force=False):
    """Step 5: the photos re-stamped on the new house (overlay.restamp)."""
    return run_step("photos", ["-m", "overlay.restamp", *(["--force"] if force else []), *stems], timings, root)


def timing_line(timings, root=ROOT):
    house = Path(root) / "house.json"
    sha = hashlib.sha256(house.read_bytes()).hexdigest()[:12] if house.is_file() else "(none)"
    return f"== house {sha}; " + ", ".join(f"{n} {t:.1f}s{'' if ok else ' (failed)'}" for n, t, ok in timings)


def _bridge(root):
    from bridge.client import Bridge
    return Bridge(root, timeout=1800)


def main(argv=None, *, root=None, bridge=None):
    """The look; returns its exit status. `root` and `bridge` stand in for the project and SketchUp in tests."""
    ap = argparse.ArgumentParser(prog="look.py", description=__doc__.splitlines()[0])
    ap.add_argument("photos", nargs="*", help="photo stems to re-match, as sources/photos/<stem>.jpg names them")
    ap.add_argument("--compile-only", action="store_true", help="stop after the compile; needs no SketchUp")
    ap.add_argument("--rebuild", action="store_true", help="build everything again, not only what changed")
    ap.add_argument("--force", action="store_true", help="re-match the named photos even when they are up to date")
    ap.add_argument("--who", default=os.environ.get("USER", "someone"), help="shown to anyone waiting for the lock")
    a = ap.parse_args(argv)
    root = Path(root or ROOT)
    timings, report, attention = [], [], False
    with lock(a.who, root / "out" / "look.lock"):
        ok = compile_steps(timings, root)
        if ok and not a.compile_only:
            ok, report, attention = sync_step(bridge or _bridge(root), timings, root, a.rebuild)
            if ok and a.photos:
                ok = photo_step(a.photos, timings, root, a.force)
    for line in report:
        print(line, flush=True)
    print(timing_line(timings, root), flush=True)
    if ok and a.photos and not a.compile_only:
        print("== overlays: " + " ".join(f"out/overlays/{p}_overlay.png" for p in a.photos), flush=True)
    return 0 if ok and not attention else 1


if __name__ == "__main__":
    raise SystemExit(main())
