"""Render a photo's request in SketchUp, refusing any model but the one its camera was solved on.

    uv run python -m overlay.render STEM [STEM ...] [--compose] [--house PATH]

A render is evidence about house.json only if SketchUp holds exactly the house.json the camera was solved against;
a render of another document through a photo camera comes back blank or unrelated, and compose would score the
wrong model. So this refuses unless house.json's sha256 is the one the camera JSON records, and asks the bridge
with that sha (Bridge.render(..., expect_sha256=sha)), so SketchUp refuses too, before drawing anything, unless its
HOUSE group was built from that document. With --compose it writes the working overlay and the clean owner overlay
afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import paths


class RenderRefused(RuntimeError):
    pass


def render_photo(stem: str, *, house_path: Path = paths.HOUSE, bridge: Any = None) -> dict:
    """Render overlay/requests/<stem>.json; RenderRefused when the camera was solved on another house.json. The
    bridge's own errors (SketchUp refusing, a timeout) pass through."""
    cam_doc = json.loads((paths.CAMERAS / f"{stem}.json").read_text())
    solved = (cam_doc.get("house") or {}).get("sha256")
    now = hashlib.sha256(Path(house_path).read_bytes()).hexdigest()
    if not solved:
        raise RenderRefused(f"{stem}: the camera records no house sha256, so nothing says which model it was solved "
                            "on; solve it again")
    if solved != now:
        raise RenderRefused(f"{stem}: the camera was solved on house.json {solved[:12]}, but it is now {now[:12]}: the "
                            f"model has moved since the solve; solve again (uv run python -m overlay.restamp {stem})")
    req_path = paths.REQUESTS / f"{stem}.json"
    req = json.loads(req_path.read_text())
    if req.get("expect_ir_sha256") != solved:
        raise RenderRefused(f"{stem}: the render request was made for another house.json; solve again")
    if bridge is None:
        from bridge.client import Bridge
        bridge = Bridge()
    result = bridge.render(req_path, expect_sha256=solved)
    if result.get("ir_sha256") not in (None, solved):
        raise RenderRefused(f"{stem}: SketchUp rendered house {str(result.get('ir_sha256'))[:12]}, not {solved[:12]}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m overlay.render",
                                 description="render photo requests in SketchUp, refusing a stale model")
    ap.add_argument("photos", nargs="+", help="photo stems")
    ap.add_argument("--house", type=Path, default=paths.HOUSE)
    ap.add_argument("--compose", action="store_true", help="write the working and clean overlays afterwards")
    a = ap.parse_args(argv)
    bad = 0
    for stem in (paths.photo_stem(p) for p in a.photos):
        try:
            out = render_photo(stem, house_path=a.house)
            print(f"{stem}: rendered {out.get('out')}")
            if a.compose:
                from .compose import compose_photo
                for clean in (False, True):
                    print(f"{stem}: wrote {compose_photo(stem, clean=clean, house_path=a.house)['overlay']}")
        except Exception as e:                       # one photo's failure is reported, the others go on
            print(f"{stem}: REFUSED: {e}" if isinstance(e, RenderRefused) else f"{stem}: ERROR: {e}", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
