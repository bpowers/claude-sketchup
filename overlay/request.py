"""Render requests from a solved camera, and the render cut back to the photo's geometry.

SketchUp renders only centred pinhole images with square pixels. A photo camera has a horizontal focal length f in
pixels, a pixel aspect a = fy / fx, a principal offset (dx, dy) and maybe a lens (k1, k2). It is rendered as a larger,
centred, square-pixel pinhole image at the same f, which the compositor turns back into the photo's geometry:

    kx = ceil(|dx|),  ky = ceil(|dy|)              (with a lens: until the canvas covers the frame undistorted, +1)
    width'  = W + 2 kx,   height' = ceil((H + 2 ky) / a),   fov_v' = 2 atan((height' / 2) / f)
    crop    = [x0, y0, W, H],  x0 = width' / 2 - W / 2 - dx,  y0 = a height' / 2 - H / 2 - dy

A model point at photo pixel (u, v) (pinhole, lens removed) lands at render pixel (u + x0, (v + y0) / a). The request
records a as "pixel_aspect" and the lens as "distortion", which render.rb ignores, and ``photo_camera(request)``
rebuilds the photo camera from it. With square pixels and no lens the offsets are rounded to whole pixels so the crop
is an exact slice (the image moves by at most half a pixel, under what a solve resolves); otherwise ``crop_render``
resamples: photo pixel (u, v) takes the render at (u' + x0, (v' + y0) / a), with (u', v') = (u, v) undistorted.

    {"out": "out/renders/living-01_edges.png", "width": 1652, "height": 1167,
     "eye": [x, y, z], "target": [x, y, z], "up": [x, y, z], "fov_v_deg": 61.2, "projection": "perspective",
     "style": "edges", "transparent": true, "hide_tags": ["zone/roof"], "hide_leaves": ["living.wall.front.door"],
     "expect_ir_sha256": "...", "crop": [26, 50, 1600, 1067], "pixel_aspect": 1.0}

Hidden tags follow where the eye stands (``default_hide_tags``): in a space of storey k, the tags zone/<z> of every
zone whose storey (house.json "zones") is greater than k, so a room's render does not show the floor above or the
roof; outside (in no space, or in a space of an outdoor zone) nothing is hidden. The space is the first whose outline
covers the eye and whose floor..ceiling covers its height (a sloped ceiling taken over the eye, a space with no ceiling
counting as SPACE_HEIGHT_IN tall). ``out`` is recorded relative to the repository root; bridge/client.py resolves it.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np
from shapely.geometry import Point, Polygon

from . import paths
from .camera import Camera, CameraError, lens_json, parse_distortion

STYLES = ("edges", "shaded")
REQUEST_KEYS = ("out", "width", "height", "eye", "target", "up", "fov_v_deg", "projection", "style", "transparent",
                "hide_tags", "crop")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_SIDE = 16384           # the largest canvas side SketchUp is asked to render
SPACE_HEIGHT_IN = 120.0    # a space with no ceiling holds an eye up to this far above its floor


class RequestError(ValueError):
    pass


# ---------------------------------------------------------------- hidden tags


def space_at(doc: Mapping[str, Any], p: Sequence[float]) -> dict | None:
    """The first space whose outline covers plan point p and whose floor..ceiling covers p's z, or None."""
    x, y, z = (float(v) for v in p)
    for sp in doc.get("elements", []):
        if not isinstance(sp, dict) or sp.get("type") != "space":
            continue
        floor = float(sp["floor_z"])
        cp = sp.get("ceiling_plane")
        if cp is not None:
            (ax, ay, az), (sx, sy) = cp["at"], cp["slope"]
            top = float(az) + float(sx) * (x - float(ax)) + float(sy) * (y - float(ay))
        elif sp.get("ceiling_z") is not None:
            top = float(sp["ceiling_z"])
        else:
            top = floor + SPACE_HEIGHT_IN
        if floor - 0.5 <= z <= top and Polygon(sp["outline"]).covers(Point(x, y)):
            return sp
    return None


def default_hide_tags(doc: Mapping[str, Any], eye: Sequence[float]) -> tuple[list[str], str]:
    """(hide_tags, words saying why) for a camera whose eye stands at eye (the module doc)."""
    at = "(" + ", ".join(f"{float(v):.1f}" for v in eye) + ")"
    zones = doc.get("zones") or {}
    space = space_at(doc, eye)
    if space is None:
        return [], f"the eye {at} stands in no space: outside the house"
    zone = space.get("zone")
    info = zones.get(zone) or {}
    if info.get("outdoor"):
        return [], f"the eye {at} stands in {space.get('id')} (zone {zone}, outdoor)"
    k = float(info.get("storey", 0))
    above = sorted((z for z, i in zones.items() if float((i or {}).get("storey", 0)) > k),
                   key=lambda z: (float(zones[z].get("storey", 0)), z))
    return [f"zone/{z}" for z in above], f"the eye {at} stands in {space.get('id')} (zone {zone}, storey {k:g})"


def check_hide_tags(tags: Any, where: str = "hide_tags") -> list[str]:
    """A list of distinct non-empty tag names, or RequestError (a bare string is one tag name, not a list)."""
    if isinstance(tags, (str, bytes)) or not isinstance(tags, (list, tuple)):
        raise RequestError(f"{where} must be a list of SketchUp tag names, got {tags!r}")
    out: list[str] = []
    for t in tags:
        if not isinstance(t, str) or not t.strip() or t in out:
            raise RequestError(f"{where} must name distinct tags as non-empty strings, got {t!r}")
        out.append(t)
    return out


# --------------------------------------------------------------------- canvas


def render_canvas(camera: Camera, *, quantize: bool = True) -> tuple[int, int, list[float], tuple[float, float]]:
    """(width', height', crop, principal offset used) of a camera's render (the module doc)."""
    a = camera.pixel_aspect
    exact = a == 1.0 and not camera.distorted
    dx, dy = camera.principal_offset
    if quantize:
        dx = float(round(dx))
        if exact:
            dy = float(round(dy))
    if camera.distorted:
        u_min, u_max, v_min, v_max = camera.replace(principal_offset=(dx, dy)).pinhole_frame_bounds()
        kx = int(math.ceil(max(abs(dx), dx - u_min, u_max - camera.width - dx))) + 1
        ky = int(math.ceil(max(abs(dy), dy - v_min, v_max - camera.height - dy))) + 1
    else:
        kx, ky = int(math.ceil(abs(dx))), int(math.ceil(abs(dy)))
    width = camera.width + 2 * kx
    height = camera.height + 2 * ky if a == 1.0 else int(math.ceil((camera.height + 2 * ky) / a - 1e-9))
    x0 = width / 2.0 - camera.width / 2.0 - dx
    y0 = a * height / 2.0 - camera.height / 2.0 - dy
    if quantize:
        x0 = int(round(x0))
        if exact:
            y0 = int(round(y0))
    if not isinstance(y0, int):
        y0 = round(float(y0), 9)
    return width, height, [x0, y0, camera.width, camera.height], (dx, dy)


def make_request(camera: Camera, out_png: str | Path, *, hide_tags: Sequence[str], style: str = "edges",
                 transparent: bool = True, hide_leaves: Sequence[str] = (), expect_ir_sha256: str | None = None,
                 target_distance: float = 120.0, quantize: bool = True) -> dict:
    """The render request for a photo camera."""
    if style not in STYLES:
        raise RequestError(f"style must be one of {STYLES}, got {style!r}")
    if Path(out_png).suffix.lower() != ".png":
        raise RequestError(f"out must be a .png path, got {out_png}")
    if expect_ir_sha256 is not None and not SHA256_RE.fullmatch(str(expect_ir_sha256)):
        raise RequestError(f"expect_ir_sha256 must be 64 lowercase hex digits, got {expect_ir_sha256!r}")
    why = camera.lens_problem()
    if why is not None:
        raise RequestError(f"no pinhole render can be warped into this photo: {why}")
    width, height, crop, used = render_canvas(camera, quantize=quantize)
    if max(width, height) > MAX_SIDE:
        raise RequestError(f"the render canvas {width}x{height} exceeds the {MAX_SIDE} px limit; the lens or the "
                           "principal offset moves the photo frame too far from the pinhole frame")
    rcam = camera.replace(principal_offset=used).centered(width, height)
    req: dict[str, Any] = {
        "out": paths.to_root(paths.from_root(out_png)), "width": width, "height": height,
        "eye": [round(float(v), 6) for v in rcam.eye],
        "target": [round(float(v), 6) for v in rcam.eye + target_distance * rcam.forward],
        "up": [round(float(v), 12) for v in rcam.up], "fov_v_deg": round(rcam.fov_v_deg, 12),
        "projection": "perspective", "style": style, "transparent": bool(transparent),
        "hide_tags": check_hide_tags(list(hide_tags)), "crop": crop,
        "pixel_aspect": round(float(camera.pixel_aspect), 12),
    }
    if hide_leaves:
        req["hide_leaves"] = list(hide_leaves)
    if camera.distorted:
        req["distortion"] = lens_json(camera)
    if expect_ir_sha256 is not None:
        req["expect_ir_sha256"] = str(expect_ir_sha256)
    return req


def _aspect(req: Mapping[str, Any]) -> float:
    a = req.get("pixel_aspect", 1.0)
    if isinstance(a, bool) or not isinstance(a, (int, float)) or not (math.isfinite(a) and a > 0):
        raise RequestError(f"render request pixel_aspect must be a positive number, got {a!r}")
    return float(a)


def _lens(req: Mapping[str, Any]) -> tuple[float, float]:
    try:
        d = req.get("distortion")
        return (0.0, 0.0) if d is None else parse_distortion(d, "request distortion")
    except CameraError as e:
        raise RequestError(str(e)) from None


def _photo_camera(req: Mapping[str, Any]) -> Camera:
    rc = Camera.look_at((req["width"], req["height"]), req["eye"], req["target"], req["up"],
                        fov_v_deg=float(req["fov_v_deg"]))
    a = _aspect(req)
    x0, y0, cw, ch = req["crop"]
    return Camera((int(cw), int(ch)), rc.f, rc.R, rc.eye,
                  (req["width"] / 2.0 - x0 - cw / 2.0, a * req["height"] / 2.0 - y0 - ch / 2.0), a, _lens(req))


def validate_request(req: Mapping[str, Any]) -> None:
    missing = [k for k in REQUEST_KEYS if k not in req]
    if missing:
        raise RequestError(f"render request is missing {missing}")
    if req["style"] not in STYLES:
        raise RequestError(f"render request style must be one of {STYLES}")
    w, h = req["width"], req["height"]
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        raise RequestError(f"render request width and height must be positive integers, got {w}, {h}")
    a = _aspect(req)
    sha = req.get("expect_ir_sha256")
    if sha is not None and not SHA256_RE.fullmatch(str(sha)):
        raise RequestError(f"render request expect_ir_sha256 must be 64 lowercase hex digits, got {sha!r}")
    check_hide_tags(req["hide_tags"], "render request hide_tags")
    crop = req["crop"]
    if len(crop) != 4:
        raise RequestError(f"crop must be [x0, y0, width, height], got {crop}")
    x0, y0, cw, ch = crop
    if x0 < 0 or y0 < 0 or x0 + cw > w + 1e-9 or y0 + ch > a * h + 1e-6:
        raise RequestError(f"crop {crop} does not fit inside the {w}x{h} render rescaled vertically by {a:g}")
    if _lens(req) != (0.0, 0.0):
        pc = _photo_camera(req)
        why = pc.lens_problem()
        if why is not None:
            raise RequestError(f"render request distortion cannot describe the photo: {why}")
        u_min, u_max, v_min, v_max = pc.pinhole_frame_bounds()
        if u_min + x0 < -1e-6 or u_max + x0 > w + 1e-6 or (v_min + y0) / a < -1e-6 or (v_max + y0) / a > h + 1e-6:
            raise RequestError(f"the {w}x{h} render does not cover the photo frame undistorted; solve again to remake "
                               "the request")


def request_camera(req: Mapping[str, Any]) -> Camera:
    """The centred, square-pixel camera SketchUp is asked to render."""
    validate_request(req)
    return Camera.look_at((req["width"], req["height"]), req["eye"], req["target"], req["up"],
                          fov_v_deg=float(req["fov_v_deg"]))


def photo_camera(req: Mapping[str, Any]) -> Camera:
    """The photo camera a request implies: principal offset, pixel aspect and lens included."""
    validate_request(req)
    return _photo_camera(req)


def crop_render(render: np.ndarray, req: Mapping[str, Any]) -> np.ndarray:
    """The photo-sized image cut out of a render: an exact slice for square pixels, a whole-pixel crop and no lens,
    otherwise resampled bilinearly (the module doc)."""
    validate_request(req)
    img = np.asarray(render)
    if img.shape[1] != req["width"] or img.shape[0] != req["height"]:
        raise RequestError(f"render is {img.shape[1]}x{img.shape[0]} but the request asked for "
                           f"{req['width']}x{req['height']}")
    x0, y0, cw, ch = req["crop"]
    cw, ch = int(cw), int(ch)
    a = _aspect(req)
    if _lens(req) != (0.0, 0.0):
        # in OpenCV positions: render column = u' + x0 - 0.5, row = (v' + y0) / a - 0.5, with (u', v') the photo pixel
        # centre undistorted
        jj, ii = np.mgrid[0:ch, 0:cw]
        p = _photo_camera(req).undistort_px(np.column_stack([ii.ravel() + 0.5, jj.ravel() + 0.5]))
        map_x = (p[:, 0] + float(x0) - 0.5).reshape(ch, cw).astype(np.float32)
        map_y = ((p[:, 1] + float(y0)) / a - 0.5).reshape(ch, cw).astype(np.float32)
        return cv2.remap(img, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    if a == 1.0 and float(x0).is_integer() and float(y0).is_integer():
        return img[int(y0):int(y0) + ch, int(x0):int(x0) + cw].copy()
    # in OpenCV positions: render column = photo column + x0, row = (photo row + 0.5 + y0) / a - 0.5
    M = np.array([[1.0, 0.0, float(x0)], [0.0, 1.0 / a, (0.5 + float(y0)) / a - 0.5]])
    return cv2.warpAffine(img, M, (cw, ch), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)


def save_request(req: Mapping[str, Any], path: str | Path) -> Path:
    validate_request(req)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(req), indent=1) + "\n")
    return p


def load_request(path: str | Path) -> dict:
    """A request, with its out resolved to an absolute path under the repository root."""
    req = json.loads(Path(path).read_text())
    if isinstance(req, dict) and isinstance(req.get("out"), str):
        req["out"] = str(paths.from_root(req["out"]))
    validate_request(req)
    return req
