"""Solve photos again on house.json as it stands, from each camera's recorded settings; render and compose them.

    uv run python -m overlay.restamp [--trial] [--force] [STEM ...]

With no stems, every camera in overlay/cameras. look.py runs this after each sync, with the photos it was given.

For each photo: its picks are resolved against house.json and solved with the settings its camera JSON records under
solve.options (overlay.solve), starting from the old camera (from many starts, as a first solve does, when the picks
changed since or the old camera's start fits clearly worse), then
- with --trial: nothing is written and nothing asks SketchUp; the line says how the camera would move
- otherwise: the camera, request and working overlay are written (unless the write guard refuses, which keeps the old
  files and says why), the request is rendered (overlay.render, which refuses unless SketchUp holds this house.json),
  and the working and clean overlays are composed.

A photo is up to date, and skipped, when its camera was solved on this house.json after its picks last changed, and
its overlays are newer than a render newer than its request and than build/build.rb and build/render.rb: solving and
rendering again would give the same images. --force re-does it anyway. One line per photo: status, rms and checks before and
after, how far the eye moved and the view turned. The exit status is 1 when any photo failed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from . import paths
from .camera import Camera, CameraError
from .features import House
from .solve import Options, guard_words, solve_photo, write_solution


def compare(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict:
    """How a camera moved: eye distance, view turn, focal length, rms and checks before and after."""
    def view(d: Mapping) -> np.ndarray:
        v = np.subtract(d["target"], d["eye"])
        return v / np.linalg.norm(v)

    def rms(d: Mapping, key: str) -> float | None:
        return (d.get(key) or {}).get("rms")

    return {"eye_moved_in": round(float(np.linalg.norm(np.subtract(new["eye"], old["eye"]))), 3),
            "view_turned_deg": round(math.degrees(math.acos(max(-1.0, min(1.0, float(view(old) @ view(new)))))), 4),
            "fov_v_deg": [old.get("fov_v_deg"), new.get("fov_v_deg")],
            "rms_px": [rms(old, "residual_px"), rms(new, "residual_px")],
            "checks_px": [rms(old, "check_residual_px"), rms(new, "check_residual_px")]}


def up_to_date(stem: str, house_sha: str) -> bool:
    """Whether solving, rendering and composing the photo again would change nothing (the module doc)."""
    cam_p = paths.CAMERAS / f"{stem}.json"
    req_p = paths.REQUESTS / f"{stem}.json"
    pick_p = paths.PICKS / f"{stem}.json"
    try:
        doc = json.loads(cam_p.read_text())
        render_p = paths.from_root(json.loads(req_p.read_text())["out"])
        overlays = [paths.OVERLAYS / f"{stem}_{k}.png" for k in ("overlay", "clean")]
        t = {p: p.stat().st_mtime for p in [cam_p, req_p, pick_p, render_p] + overlays}
        drawn = max((p.stat().st_mtime for p in paths.BUILDERS if p.is_file()), default=0.0)
    except (OSError, KeyError, ValueError):
        return False
    return ((doc.get("house") or {}).get("sha256") == house_sha and t[pick_p] <= t[cam_p] and
            t[render_p] >= max(t[req_p], drawn) and all(t[o] >= t[render_p] for o in overlays))


def restamp(stem: str, *, house: House, trial: bool, force: bool = False, bridge: Any = None) -> dict:
    """Re-stamp one photo; returns its summary (status and comparison)."""
    info: dict[str, Any] = {"stem": stem}
    if not trial and not force and up_to_date(stem, house.sha256):
        return dict(info, status="up to date")
    try:
        cam_path = paths.CAMERAS / f"{stem}.json"
        old = json.loads(cam_path.read_text())
        options = Options.from_json((old.get("solve") or {}).get("options") or {})
        sol = None
        if paths.PICKS.joinpath(f"{stem}.json").stat().st_mtime <= cam_path.stat().st_mtime:
            # the picks are the ones the old camera was solved from, so it starts the re-solve
            try:
                sol = solve_photo(stem, options, house=house, init=Camera.from_json(old))
            except CameraError:
                sol = None
            old_rms = (old.get("residual_px") or {}).get("rms") or 0.0
            if sol is not None and sol.doc["residual_px"]["rms"] > 2.0 * old_rms + 1.0:
                sol = None                           # it fits clearly worse: solve from many starts instead
        if sol is None:
            sol = solve_photo(stem, options, house=house)
    except Exception as e:                           # a report, never a reason to lose the other photos
        return dict(info, status=f"solve failed: {e}")
    info.update(compare(old, sol.doc))
    if sol.guard["refused"]:
        return dict(info, status="refused by the write guard (old camera kept): " + guard_words(sol.guard))
    if trial:
        return dict(info, status="trial")
    try:
        write_solution(sol, house_path=house.path)
        from .compose import compose_photo
        from .render import render_photo
        render_photo(stem, house_path=house.path, bridge=bridge)
        for clean in (False, True):
            compose_photo(stem, clean=clean, house_path=house.path)
    except Exception as e:
        return dict(info, status=f"failed: {type(e).__name__}: {e}")
    return dict(info, status="ok")


def words(info: Mapping[str, Any]) -> str:
    out = f"{info['stem']}: {info['status']}"
    if "eye_moved_in" in info:
        r, c = info["rms_px"], info["checks_px"]
        fmt = (lambda v: "-" if v is None else f"{v:.2f}")
        out += (f"; rms {fmt(r[0])} -> {fmt(r[1])} px, checks {fmt(c[0])} -> {fmt(c[1])} px, eye moved "
                f"{info['eye_moved_in']:.2f} in, view turned {info['view_turned_deg']:.3f} deg")
    return out


def main(argv: Sequence[str] | None = None, echo: Callable[[str], Any] = print) -> int:
    ap = argparse.ArgumentParser(prog="python -m overlay.restamp",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("photos", nargs="*", help="photo stems (default: every camera in overlay/cameras)")
    ap.add_argument("--trial", action="store_true", help="solve only and report the movement; write nothing")
    ap.add_argument("--force", action="store_true", help="re-do photos that are up to date")
    ap.add_argument("--house", type=Path, default=paths.HOUSE)
    a = ap.parse_args(argv)
    stems = [paths.photo_stem(p) for p in a.photos] or sorted(p.stem for p in paths.CAMERAS.glob("*.json"))
    missing = [s for s in stems if not (paths.CAMERAS / f"{s}.json").exists()]
    if missing:
        print(f"ERROR: no camera for {', '.join(missing)}; solve it first (uv run python -m overlay.solve STEM)",
              file=sys.stderr)
        return 2
    house = House.load(a.house)
    bad = 0
    for stem in stems:
        t0 = time.monotonic()
        info = restamp(stem, house=house, trial=a.trial, force=a.force)
        echo(words(info) + f" ({time.monotonic() - t0:.1f} s)")
        bad += info["status"] not in ("ok", "trial", "up to date")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
