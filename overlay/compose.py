"""Lay a SketchUp render over its photo and measure how well they agree.

    uv run python -m overlay.compose STEM [--clean] [--camera PATH] [--request PATH] [--render PATH] [--out PNG]

Reads overlay/cameras/<stem>.json and overlay/requests/<stem>.json (whose "out" names the render). Writes
out/overlays/<stem>_overlay.png and its numbers, out/overlays/<stem>_overlay.json.

The working overlay dims and desaturates the photo, paints the render's edges in magenta, and marks every pick:
a yellow ring is the pick (magenta when the solve called it an outlier, a green diamond when it is a held-out check),
grey shows where it was before snapping, cyan is the model feature projected (a cross, or a line for a line pick)
and red the residual (projected minus observed). A header box gives the solve's rms, the checks and the chamfer.

--clean writes out/overlays/<stem>_clean.png instead: the model's edges on the dimmed photo and nothing else, for
the owner. It refuses when there is no usable render, since a clean picture without model edges would look like a
result.

The render is cut to the photo's frame (request.crop_render: the pixel aspect rescaled, the lens warped in), so edges
land where the lens put them. A request whose implied photo camera does not reproject the picks to within 0.75 px of
the camera JSON is refused. A render older than its request (the camera was solved again since) is stale: reported,
not drawn, not scored.

Chamfer (correspondence-free): directed distances between the render's edges and the photo's (Canny) edges,
truncated at tau px. render_to_photo_px, the mean distance from each render edge pixel to the nearest photo edge, is
the direction that says whether the model is where the photo says (the render has no furniture to match);
photo_to_render_px counts photo edges near the model and grows with clutter. *_within_near: the fraction within
near px.

With house.json, every pick's reference is resolved again: one that moved more than 0.05 in since the solve is
reported (solve again).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import paths
from .camera import Camera, Correspondence, line_through, target_point
from .features import FeatureError, House
from .picks import reresolve
from .request import RequestError, crop_render, load_request, photo_camera

MAGENTA = (255, 0, 220)
OBSERVED, PROJECTED, RESIDUAL = (255, 230, 0), (0, 255, 255), (255, 40, 40)
SNAP_MOVE, OUTLIER, CHECK = (170, 170, 170), (255, 0, 160), (120, 255, 120)
# an antialiased 1 px line between two pixel columns reaches only about 0.44 in each, so a render edge is any pixel
# at a quarter of full strength or more
RENDER_EDGE_THRESHOLD = 0.25
PAINT_GAIN = 1.4           # each pixel is painted min(1, gain x the render's edge strength), so faint edges show
MOVED_IN = 0.05
MATCH_TOL_PX = 0.75
CAMERA_KEYS = ("label", "model", "px", "px_approx", "snap_quality", "sigma_px", "residual_px", "inlier", "direction")


class ComposeError(ValueError):
    pass


def font(size: int = 14) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def text_halo(d: ImageDraw.ImageDraw, xy, s: str, fill, fnt) -> None:
    x, y = xy
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                d.text((x + dx, y + dy), s, fill=(0, 0, 0), font=fnt)
    d.text((x, y), s, fill=fill, font=fnt)


# ----------------------------------------------------------------------- edges


def load_render(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ComposeError(f"cannot read render {path}")
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA if img.shape[2] == 4 else cv2.COLOR_BGR2RGB)
    return img


def render_edges(render: np.ndarray) -> np.ndarray:
    """Edge strength in [0, 1] from an 'edges' render: dark pixels are edges. A transparent render is read as
    premultiplied (strength = alpha - luminance), as SketchUp writes it, so the antialiased silhouette of a face drawn
    in the background colour against empty space reads as no edge rather than a faint grey one."""
    img = np.asarray(render)
    if img.ndim == 2:
        return np.clip(1.0 - img.astype(float) / 255.0, 0.0, 1.0)
    lum = cv2.cvtColor(np.ascontiguousarray(img[..., :3]), cv2.COLOR_RGB2GRAY).astype(float) / 255.0
    strength = 1.0 - lum if img.shape[2] == 3 else img[..., 3].astype(float) / 255.0 - lum
    return np.clip(strength, 0.0, 1.0)


def photo_edges(image: np.ndarray, *, sigma: float = 1.2, high_percentile: float = 90.0,
                low_ratio: float = 0.4) -> np.ndarray:
    """Canny edges, with thresholds from the photo's own gradient distribution."""
    g = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image, (0, 0), sigma)
    mag = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    hi = max(float(np.percentile(mag, high_percentile)), 1.0)
    return cv2.Canny(g, low_ratio * hi, hi, L2gradient=True) > 0


def chamfer(photo_edge: np.ndarray, render_edge: np.ndarray, *, tau: float = 20.0, near: float = 3.0) -> dict:
    """Directed, truncated chamfer distances between two binary edge maps (the module doc)."""
    pe, re_ = np.asarray(photo_edge, bool), np.asarray(render_edge, bool)
    if pe.shape != re_.shape:
        raise ComposeError(f"edge maps differ in size: {pe.shape} vs {re_.shape}")
    out: dict[str, Any] = {"tau_px": tau, "near_px": near, "n_render_px": int(re_.sum())}
    if not re_.any():
        return dict(out, render_to_photo_px=None, render_within_near=None, photo_to_render_px=None,
                    photo_within_near=None, note="no render edges")
    dt_photo = cv2.distanceTransform((~pe).astype(np.uint8), cv2.DIST_L2, 5)
    dt_render = cv2.distanceTransform((~re_).astype(np.uint8), cv2.DIST_L2, 5)
    dr = dt_photo[re_]
    out.update(render_to_photo_px=float(np.minimum(dr, tau).mean()), render_within_near=float((dr <= near).mean()))
    P = pe & (dt_render <= tau)
    if P.any():
        dp = dt_render[P]
        out.update(photo_to_render_px=float(dp.mean()), photo_within_near=float((dp <= near).mean()))
    else:
        out.update(photo_to_render_px=None, photo_within_near=None)
    return out


# -------------------------------------------------------------------- drawing


def overlay_image(photo: np.ndarray, strength: np.ndarray | None, colour: Sequence[int] = MAGENTA,
                  dim: float = 0.62) -> np.ndarray:
    """The dimmed, desaturated photo with the render's edges painted on it."""
    gray = cv2.cvtColor(photo, cv2.COLOR_RGB2GRAY).astype(float)[..., None]
    base = dim * (0.65 * gray + 0.35 * photo.astype(float))
    if strength is not None:
        a = cv2.dilate(strength.astype(np.float32), np.ones((2, 2), np.uint8))
        a = np.clip(PAINT_GAIN * a, 0.0, 1.0)[..., None]
        base = base * (1 - a) + np.asarray(colour, float)[None, None, :] * a
    return np.clip(base, 0, 255).astype(np.uint8)


def line_curve(camera: Camera, X0: Sequence[float], direction: Sequence[float],
               samples: int = 160) -> list[tuple[float, float]]:
    """Photo pixels along the projection of the 3D line X0 + t direction across the frame (a sampled curve under
    distortion); empty when it misses the frame."""
    a, b, c = camera.project_line(np.asarray(X0, dtype=float), direction)[0]
    u0, u1, v0, v1 = camera.pinhole_frame_bounds()
    pts = []
    if abs(b) > 1e-12:
        pts += [(u, -(a * u + c) / b) for u in (u0, u1) if v0 - 1e-6 <= -(a * u + c) / b <= v1 + 1e-6]
    if abs(a) > 1e-12:
        pts += [(-(b * v + c) / a, v) for v in (v0, v1) if u0 - 1e-6 <= -(b * v + c) / a <= u1 + 1e-6]
    if len(pts) < 2:
        return []
    pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in pts))
    p, q = np.array(pts[0]), np.array(pts[-1])
    if not camera.distorted:
        return [tuple(p), tuple(q)]
    t = np.linspace(0.0, 1.0, max(2, int(samples)))[:, None]
    return [tuple(v) for v in camera.distort_px(p + t * (q - p))]


def draw_picks(im: Image.Image, corrs: Sequence[Correspondence], camera: Camera,
               inliers: Sequence[bool] | None = None) -> None:
    """Picks (rings, numbered), snap moves, projections and residual vectors (the module doc's colours)."""
    d = ImageDraw.Draw(im)
    fnt = font(12)
    for k, c in enumerate(corrs):
        obs = c.px
        if c.px_approx is not None:
            d.line([c.px_approx, obs], fill=SNAP_MOVE, width=1)
        if c.is_point:
            uv, z = camera.project(np.asarray(c.model, dtype=float), return_depth=True)
            if z > 0:
                d.line([uv[0] - 6, uv[1], uv[0] + 6, uv[1]], fill=PROJECTED, width=2)
                d.line([uv[0], uv[1] - 6, uv[0], uv[1] + 6], fill=PROJECTED, width=2)
                d.line([obs, tuple(uv)], fill=RESIDUAL, width=2)
        else:
            X0, e = line_through(c.model, c.direction, camera.eye)
            curve = line_curve(camera, X0, e)
            if len(curve) >= 2:
                d.line(curve, fill=PROJECTED, width=1)
            foot, _, _ = camera.line_foot(np.array([c.px], dtype=float), X0, e)
            d.line([obs, tuple(foot[0])], fill=RESIDUAL, width=2)
        box = [obs[0] - 5, obs[1] - 5, obs[0] + 5, obs[1] + 5]
        if c.role == "check":
            ring = CHECK
            diamond = [(obs[0], obs[1] - 6), (obs[0] + 6, obs[1]), (obs[0], obs[1] + 6), (obs[0] - 6, obs[1])]
            d.line(diamond + [diamond[0]], fill=ring, width=2)
        else:
            ring = OUTLIER if inliers is not None and not inliers[k] else OBSERVED
            d.ellipse(box, outline=(0, 0, 0), width=3)
            d.ellipse(box, outline=ring, width=2)
        text_halo(d, (obs[0] + 8, obs[1] - 7), str(k + 1), ring, fnt)


def _header(im: Image.Image, stats: Mapping[str, Any]) -> None:
    lines = [stats["photo"]]
    r, ck, ch = stats.get("residual_px"), stats.get("check_residual_px"), stats.get("chamfer")
    if r:
        lines.append(f"picks: rms {r['rms']:.2f} px, max {r['max']:.2f} px, n {r['n']}")
    if ck:
        lines.append(f"held-out checks (green diamonds): rms {ck['rms']:.2f} px, max {ck['max']:.2f} px, n {ck['n']}")
    if ch and ch.get("render_to_photo_px") is not None:
        lines.append(f"chamfer render->photo {ch['render_to_photo_px']:.2f} px "
                     f"({100 * ch['render_within_near']:.0f}% within {ch['near_px']:.0f} px)")
    lines.append("magenta = model edges; yellow = picks (numbered as in the camera JSON); cyan = projections; "
                 "red = residuals")
    lines += ["! " + w for w in stats.get("warnings", [])]
    fnt = font(13)
    d = ImageDraw.Draw(im, "RGBA")
    width = max(d.textlength(s, font=fnt) for s in lines) + 16
    d.rectangle([6, 6, 6 + width, 10 + 17 * len(lines)], fill=(0, 0, 0, 170))
    for i, s in enumerate(lines):
        text_halo(d, (14, 10 + 17 * i), s, (240, 240, 240), fnt)


# ------------------------------------------------------------------- checks


def picks_from_camera_json(doc: Mapping[str, Any]) -> tuple[list[Correspondence], list[bool]]:
    corrs, inl = [], []
    for c in doc.get("correspondences", []):
        corrs.append(Correspondence(c["label"], tuple(c["px"]), tuple(c["model"]), sigma_px=c.get("sigma_px", 2.0),
                                    px_approx=tuple(c["px_approx"]) if c.get("px_approx") else None,
                                    extra={k: v for k, v in c.items() if k not in CAMERA_KEYS},
                                    direction=c.get("direction")))
        inl.append(c.get("inlier") is not False)
    return corrs, inl


def check_request_matches_camera(req: Mapping[str, Any], cam: Camera, corrs: Sequence[Correspondence]) -> float:
    """The largest pixel distance between the request's photo camera and the camera JSON at the points the picks
    show (each at least 1 in in front of the camera); ComposeError above MATCH_TOL_PX."""
    pc = photo_camera(req)
    if pc.image_size != cam.image_size:
        raise ComposeError(f"the request's crop {pc.image_size} differs from the camera's image size {cam.image_size}")
    pts = np.array([target_point(cam, c.px, c.model, c.direction) for c in corrs], dtype=float).reshape(-1, 3)
    pts = pts[cam.to_camera_frame(pts)[:, 2] >= 1.0] if len(pts) else pts
    if not len(pts):
        pts = (cam.eye + 100.0 * cam.forward)[None, :]
    gaps = np.linalg.norm(pc.project(pts) - cam.project(pts), axis=1)
    err = float(np.nanmax(gaps)) if np.isfinite(gaps).any() else float("inf")
    if not err <= MATCH_TOL_PX:
        raise ComposeError(f"the render request does not match the camera ({err:.2f} px apart); solve again to "
                           "remake the request")
    return err


def moved_picks(cam_doc: Mapping[str, Any], house: House) -> list[str]:
    """Warnings for picks whose references no longer resolve, or moved more than MOVED_IN, since the solve."""
    warns = []
    solved = (cam_doc.get("house") or {}).get("sha256")
    if solved and solved != house.sha256:
        warns.append(f"house.json changed since the solve ({solved[:12]} -> {house.sha256[:12]})")
    for c in cam_doc.get("correspondences", []):
        model, err = reresolve(c, house)
        if err:
            warns.append(f"'{c['label']}' no longer resolves against house.json: {err}")
        elif model is not None:
            moved = [abs(a - b) for a, b in zip(model, c["model"]) if a is not None and b is not None]
            if any((a is None) != (b is None) for a, b in zip(model, c["model"])) or (moved and max(moved) > MOVED_IN):
                warns.append(f"'{c['label']}' ({', '.join(c.get('model_src', {}))}) moved {max(moved or [0.0]):.2f} in "
                             "since the camera was solved: solve again")
    return warns


# -------------------------------------------------------------------- compose


def compose(photo_path: Path, camera_path: Path, request_path: Path | None, out_png: Path, *,
            render_path: Path | None = None, house_path: Path | None = None, clean: bool = False) -> dict:
    """Write the working overlay and its statistics JSON, or with clean only the model's edges on the dimmed photo.
    Returns the statistics."""
    photo = paths.load_rgb(photo_path)
    h, w = photo.shape[:2]
    stats: dict[str, Any] = {"photo": Path(photo_path).name,
                             "generated": _dt.datetime.now().isoformat(timespec="seconds"), "warnings": []}
    cam_doc = json.loads(Path(camera_path).read_text())
    cam = Camera.from_json(cam_doc)
    if cam.image_size != (w, h):
        raise ComposeError(f"the camera's image_size {cam.image_size} but {Path(photo_path).name} is {w}x{h}")
    corrs, inliers = picks_from_camera_json(cam_doc)
    stats.update(camera=paths.to_root(camera_path), residual_px=cam_doc.get("residual_px"),
                 check_residual_px=cam_doc.get("check_residual_px"))
    strength = None
    if request_path is not None and Path(request_path).exists():
        req = load_request(request_path)
        rpath = Path(render_path or req["out"])
        stats.update(request=paths.to_root(request_path), render=paths.to_root(rpath),
                     request_camera_mismatch_px=round(check_request_matches_camera(req, cam, corrs), 4))
        if not rpath.exists():
            stats["warnings"].append(f"no render at {paths.to_root(rpath)} yet: overlay shows the picks only")
        elif Path(request_path).stat().st_mtime > rpath.stat().st_mtime:
            stats["stale"] = True
            stats["warnings"].append(f"render {rpath.name} is older than its request (the camera was solved again): "
                                     "render again; overlay shows the picks only")
        else:
            try:
                strength = render_edges(crop_render(load_render(rpath), req))
            except RequestError as e:
                stats["warnings"].append(f"render {rpath.name} was made for another request ({e}): render again")
    else:
        stats["warnings"].append("no render request: overlay shows the picks only")
    if house_path is not None and Path(house_path).exists():
        try:
            stats["warnings"] += moved_picks(cam_doc, House.load(house_path))
        except FeatureError as e:
            stats["warnings"].append(f"cannot read {house_path}: {e}")
    if strength is not None:
        stats["chamfer"] = chamfer(photo_edges(photo), strength > RENDER_EDGE_THRESHOLD)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if clean:
        if strength is None:
            raise ComposeError(f"no usable render for {Path(photo_path).name}, so a clean overlay would show no model: "
                               + "; ".join(stats["warnings"]))
        Image.fromarray(overlay_image(photo, strength)).save(out_png)
        return dict(stats, overlay=paths.to_root(out_png), clean=True)
    im = Image.fromarray(overlay_image(photo, strength))
    draw_picks(im, corrs, cam, inliers)
    _header(im, stats)
    im.save(out_png)
    stats["overlay"] = paths.to_root(out_png)
    out_png.with_suffix(".json").write_text(json.dumps(stats, indent=1) + "\n")
    return stats


def compose_photo(stem: str, *, clean: bool = False, house_path: Path = paths.HOUSE) -> dict:
    """compose with the default files for a photo stem."""
    cam_path = paths.CAMERAS / f"{stem}.json"
    doc = json.loads(cam_path.read_text())
    return compose(paths.resolve_photo(doc.get("photo") or stem), cam_path, paths.REQUESTS / f"{stem}.json",
                   paths.OVERLAYS / f"{stem}_{'clean' if clean else 'overlay'}.png", house_path=house_path,
                   clean=clean)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m overlay.compose",
                                 description="overlay a SketchUp render on its photo")
    ap.add_argument("photo", help="photo stem")
    ap.add_argument("--camera", type=Path)
    ap.add_argument("--request", type=Path)
    ap.add_argument("--render", type=Path)
    ap.add_argument("--house", type=Path, default=paths.HOUSE)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--clean", action="store_true", help="the model's edges on the dimmed photo, nothing else")
    a = ap.parse_args(argv)
    stem = paths.photo_stem(a.photo)
    cam_path = a.camera or paths.CAMERAS / f"{stem}.json"
    try:
        doc = json.loads(Path(cam_path).read_text())
        stats = compose(paths.resolve_photo(doc.get("photo") or stem), cam_path,
                        a.request or paths.REQUESTS / f"{stem}.json",
                        a.out or paths.OVERLAYS / f"{stem}_{'clean' if a.clean else 'overlay'}.png",
                        render_path=a.render, house_path=a.house, clean=a.clean)
    except (OSError, ComposeError, RequestError) as e:
        print(f"ERROR: {e}")
        return 2
    print(f"wrote {stats['overlay']}")
    ch = stats.get("chamfer") or {}
    if ch.get("render_to_photo_px") is not None:
        print(f"chamfer render->photo {ch['render_to_photo_px']:.2f} px ({100 * ch['render_within_near']:.0f}% within "
              f"{ch['near_px']:.0f} px)")
    for wmsg in stats["warnings"]:
        print("note: " + wmsg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
