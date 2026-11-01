"""Solve a photo's camera from its picks; write the camera JSON, the render request and a working overlay.

    uv run python -m overlay.solve STEM
        [--attitude auto|free|zero_pitch|level] [--principal free|dy|fixed] [--principal-offset DX,DY]
        [--principal-prior PX] [--focal F] [--aspect free|fixed] [--pixel-aspect A] [--aspect-bounds LO,HI]
        [--distortion none|fixed|k1|k1k2] [--k1 K1] [--k2 K2] [--k1-prior M,S] [--k2-prior M,S]
        [--loss L] [--f-scale S] [--outlier-threshold T] [--fov-h-bounds LO,HI] [--start CAMERA.json]
        [--no-snap] [--snap-radius PX] [--use-lines] [--hide-tags TAGS] [--house PATH] [--picks PATH]
        [--dry-run] [--force]

1. Load overlay/picks/<stem>.json, resolving every reference against house.json, and snap the picks marked snap.
2. Detect line segments and test the verticals (vanishing.analyze_verticals). --attitude auto takes 'level' when
   they are parallel and the roll is under 0.3 degrees, 'zero_pitch' when parallel but rolled, 'free' otherwise.
   With parallel verticals the horizontal vanishing points give the horizon, whose principal dy starts the solve
   unless --principal-offset is given.
3. Solve (camera.solve_camera). Picks with role "check" are held out and measured with the solved camera.
   --use-lines adds the photo's long segments as rows that point them at their building axis' vanishing point:
   whole vanishing-point families are labelled by a rough camera (a solve with the focal length held at a 70 degree
   field of view, or a re-solve's old camera), the camera is solved with them, single segments are classified again
   by that camera, and it is solved once more. Segments hold the attitude, focal length and principal point to a
   fraction of a pixel.
4. The write guard (write_guard) refuses a camera the picks do not pin down, naming every reason: a focal length
   uncertain by more than 20%; an eye distance along the view uncertain by more than the larger of 36 in and 3% of
   the picks' median depth; a pixel aspect uncertain by more than 0.03; a direction no data constrains; no
   redundancy to judge the fit by; full points that lie on a line. It says what in the picks' layout causes a
   refusal (full points on one plane, picks bunched in part of the frame). Strongly correlated parameters are a
   warning, not a refusal: each one's uncertainty already counts the correlation. Nothing is written unless --force.
5. Write overlay/cameras/<stem>.json, overlay/requests/<stem>.json (hide tags by where the eye stands, unless
   --hide-tags names them; the picks' open_leaves as hide_leaves) and out/overlays/<stem>_overlay.png. --dry-run
   prints the result and writes nothing.

The camera JSON records the house.json sha256 the picks were resolved against, and under solve.options every setting
of this command as it resolved (the attitude auto chose, the starting principal offset), so overlay.restamp solves it
again the same way.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from . import paths
from .camera import (ASPECT_BOUNDS, DISTORTIONS, K1_PRIOR, K2_PRIOR, Camera, CameraError, SolveResult,
                     camera_start_doc, correspondence_doc, parse_start_camera, residual_vectors, solve_camera)
from .features import FeatureError, House
from .picks import PicksError, load, picks_path, snap_all
from .request import RequestError, default_hide_tags, make_request, save_request, space_at
from .vanishing import (analyze_verticals, assign_clusters, classify_segments, detect_segments,
                        detect_vanishing_points, horizon_dy)

# the write guard's limits (module doc, step 4)
FOCAL_STD_LIMIT_FRAC = 0.20
# The eye's one-sigma uncertainty along the view: an error of 3% scales the image about the principal point by 3%.
# Every interior (median pick depth under 1200 in) keeps the 36 in floor; a far exterior is judged by its distance.
EYE_STD_LIMIT_IN = 36.0
EYE_STD_LIMIT_FRAC = 0.03
# 0.03 moves a feature 100 px from the principal point by 3 px
ASPECT_STD_LIMIT = 0.03


@dataclass
class Options:
    """Every setting of a solve: the command's options, as the camera JSON records them under solve.options."""

    attitude: str = "auto"
    principal: str = "free"
    principal_offset: list[float] | None = None
    principal_prior_px: float | None = None
    focal: float | None = None
    aspect: str = "fixed"
    pixel_aspect: float = 1.0
    aspect_bounds: list[float] = field(default_factory=lambda: list(ASPECT_BOUNDS))
    distortion: str = "none"
    k1: float = 0.0
    k2: float = 0.0
    k1_prior: list[float] = field(default_factory=lambda: list(K1_PRIOR))
    k2_prior: list[float] = field(default_factory=lambda: list(K2_PRIOR))
    loss: str = "soft_l1"
    f_scale: float = 1.5
    outlier_threshold: float = 3.0
    fov_h_bounds: list[float] = field(default_factory=lambda: [10.0, 140.0])
    start: dict | None = None
    snap: bool = True
    snap_radius: float = 12.0
    use_lines: bool = False
    segment_sigma: float = 1.5
    max_segments: int = 60
    hide_tags: list[str] | None = None

    def to_json(self) -> dict:
        return json.loads(json.dumps(asdict(self)))

    @classmethod
    def from_json(cls, d: dict) -> "Options":
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown solve options {sorted(unknown)}")
        return cls(**d)

    def solve_kwargs(self) -> dict:
        """camera.solve_camera's keywords for these options (the attitude and principal offset already resolved)."""
        kw: dict[str, Any] = dict(
            attitude=self.attitude, principal=self.principal,
            principal_offset=tuple(self.principal_offset or (0.0, 0.0)), principal_prior_px=self.principal_prior_px,
            aspect=self.aspect, pixel_aspect=self.pixel_aspect, aspect_bounds=tuple(self.aspect_bounds),
            distortion=self.distortion, k1=self.k1, k2=self.k2, k1_prior=tuple(self.k1_prior),
            k2_prior=tuple(self.k2_prior), loss=self.loss, f_scale=self.f_scale,
            outlier_threshold=self.outlier_threshold, fov_h_bounds_deg=tuple(self.fov_h_bounds),
            start_camera=self.start)
        if self.focal:
            kw.update(focal="fixed", f=float(self.focal))
        return kw


@dataclass
class Solution:
    """A solved photo, not yet written: the camera JSON, its render request, the guard's verdict and the log."""

    stem: str
    photo: Path
    doc: dict
    request: dict
    guard: dict
    log: list[str]


def write_guard(result: SolveResult) -> dict:
    """What the write guard measures, its limits, every reason it refuses the camera and, when it refuses, what in the
    picks' layout causes it (module doc, step 4)."""
    cam, std, geo = result.camera, result.std, result.geometry
    depth = float(result.median_depth_in)
    eye_limit = max(EYE_STD_LIMIT_IN, EYE_STD_LIMIT_FRAC * depth)
    f_std, along, a_std = std.get("f"), std.get("eye_along_view"), std.get("pixel_aspect")
    refused = []
    if geo.get("collinear"):
        refused.append("the full points lie nearly on a line, so the pose is not determined")
    if f_std is not None and f_std > FOCAL_STD_LIMIT_FRAC * cam.f:
        refused.append(f"focal length uncertain by {100 * f_std / cam.f:.0f}% "
                       f"(limit {100 * FOCAL_STD_LIMIT_FRAC:.0f}%)")
    if along is not None and along > eye_limit:
        refused.append(f"eye distance along the view uncertain by {along:.0f} in (limit {eye_limit:.0f})")
    if a_std is not None and a_std > ASPECT_STD_LIMIT:
        refused.append(f"pixel aspect uncertain by {a_std:.3f} (limit {ASPECT_STD_LIMIT:g})")
    if result.degenerate:
        refused.append("no data constrains " + ", ".join(result.degenerate))
    if not math.isfinite(result.sigma_ratio):
        refused.append("no redundancy: the solve has no more rows than parameters, so nothing judges its fit")
    causes = []
    if refused and geo.get("coplanar"):
        causes.append("the full points lie nearly on one plane, so the free intrinsics trade against the pose: add "
                      "picks off that plane (a floor or ceiling line, a corner of another wall)")
    cover = geo.get("coverage")
    if refused and cover and cover[0] * cover[1] < 0.2:
        causes.append(f"the picks cover only {100 * cover[0]:.0f}% x {100 * cover[1]:.0f}% of the frame: spread them "
                      "to its edges")

    def num(v: Any) -> Any:
        return round(float(v), 6) if isinstance(v, (int, float)) and math.isfinite(float(v)) else None

    return {"limits": {"f_std_frac": FOCAL_STD_LIMIT_FRAC, "eye_std_in": round(eye_limit, 3),
                       "pixel_aspect_std": ASPECT_STD_LIMIT},
            "values": {"f_std_px": num(f_std), "eye_along_view_std_in": num(along), "median_depth_in": num(depth),
                       "pixel_aspect_std": num(a_std), "sigma_ratio": num(result.sigma_ratio)},
            "refused": refused, "causes": causes}


def guard_words(guard: dict) -> str:
    """The guard's refusals, and their causes, in one line."""
    return "; ".join(guard["refused"]) + (" (" + "; ".join(guard["causes"]) + ")" if guard.get("causes") else "")


def solve_photo(stem: str, options: Options, *, house: House, picks: str | Path | None = None,
                init: Camera | None = None) -> Solution:
    """Solve one photo (module doc, steps 1 to 4). With init (a re-solve's old camera) the solve starts from it alone
    instead of from many starts. Raises PicksError, CameraError or OSError when it cannot."""
    opts = Options.from_json(options.to_json())
    pset = load(picks or stem, house)
    photo = paths.resolve_photo(pset.photo)
    if paths.photo_stem(photo) != stem:
        raise PicksError(f"{picks_path(picks or stem)} names photo {pset.photo}, not {stem}")
    img = paths.load_rgb(photo)
    h, w = img.shape[:2]
    log = [f"{stem}: {len(pset.points)} picks ({sum(c.is_point for c in pset.points)} points, "
           f"{sum(not c.is_point for c in pset.points)} lines; {len(pset.checks)} held out as checks)"]
    snap_warnings: list[str] = []
    if opts.snap:
        pset, snap_warnings = snap_all(pset, img, radius=opts.snap_radius)
        log += [f"  snap {label}: {'ok' if r.ok else 'FAILED'} q={r.quality:.2f} moved {r.moved_px:.1f} px "
                f"({r.method}; {r.detail})" for label, r in pset.snaps.items()]
    log += ["WARNING: " + s for s in snap_warnings]
    if opts.start is not None:
        parse_start_camera(opts.start, (w, h))

    segs = detect_segments(img, min_length=25.0)
    va = analyze_verticals(segs, (w, h), f=opts.focal)
    rest = np.delete(np.arange(len(segs)), va.indices)
    clusters = detect_vanishing_points(segs[rest], (w, h), count=3)
    for c in clusters:                                   # indices into segs, not into the remainder
        c.indices = [int(rest[i]) for i in c.indices]
    log.append(f"verticals: {va.state} (n {va.n_segments}, pitch {va.pitch_deg} deg for f={va.f_assumed:.0f}, "
               f"95% {va.pitch_ci95_deg}, roll {va.roll_deg})")
    if opts.attitude == "auto":
        opts.attitude = "free" if va.state != "parallel" else "level" if abs(va.roll_deg or 0.0) < 0.3 else "zero_pitch"
    dy_h = horizon_dy(clusters, (w, h), va.roll_deg or 0.0) if va.state == "parallel" else None
    if opts.principal_offset is None:
        opts.principal_offset = [0.0, 0.0 if dy_h is None else round(dy_h, 3)]
        if dy_h is not None:
            log.append(f"horizon: starting principal dy {dy_h:.1f} px")
    log.append(f"solve: attitude {opts.attitude}, principal {opts.principal}, start offset {opts.principal_offset}")

    kw = opts.solve_kwargs()
    if init is not None:
        kw.update(init=init, multistart=False)
    points = pset.solve_points
    if opts.use_lines:
        def capped(groups: dict) -> dict:
            # segments come longest first, so the lowest indices are the longest; the cap keeps hundreds of short
            # texture segments from outvoting the picks
            return {k: segs[np.sort(v)[:opts.max_segments]] for k, v in groups.items() if len(v)}

        rough = init
        if rough is None:
            f_prov = opts.focal or (w / 2.0) / np.tan(np.radians(35.0))
            rough = solve_camera(points, (w, h), **dict(kw, focal="fixed", f=f_prov)).camera
        groups = assign_clusters(segs, clusters, rough)
        groups["z"] = np.union1d(groups["z"], np.asarray(va.indices, dtype=int))
        result = solve_camera(points, (w, h), segments=capped(groups), segment_sigma_px=opts.segment_sigma,
                              **dict(kw, init=rough))
        seg_groups = capped(classify_segments(segs, result.camera, threshold_deg=1.5))
        result = solve_camera(points, (w, h), segments=seg_groups, segment_sigma_px=opts.segment_sigma,
                              **dict(kw, init=result.camera))
        log.append("segments: " + ", ".join(f"{k} {len(v)}" for k, v in seg_groups.items()))
    else:
        result = solve_camera(points, (w, h), **kw)
    log.append(result.summary())
    cam = result.camera

    check_docs, check_norms = [], []
    if pset.checks:
        vec, depth = residual_vectors(pset.checks, cam)
        log.append("held-out checks (not in the solve), residual with the solved camera:")
        for c, r, z in zip(pset.checks, vec, depth):
            n = float(np.linalg.norm(r))
            kind = "point" if c.is_point else "line " + "xyz"[c.free_axis]
            log.append(f"  {n:7.2f} px  sigma {c.sigma_px:.1f}  {kind:6s}  {c.label}"
                       + ("  BEHIND THE CAMERA" if c.is_point and z <= 0 else ""))
            check_docs.append(correspondence_doc(c, r, None))
            check_norms.append(n)
    if opts.hide_tags is None:
        tags, why = default_hide_tags(house.doc, cam.eye)
        log.append(f"hide tags {', '.join(tags) or 'none'}: {why}")
    else:
        tags = list(opts.hide_tags)
        log.append(f"hide tags {', '.join(tags) or 'none'}, as given")
    guard = write_guard(result)
    space = space_at(house.doc, cam.eye)
    extra: dict[str, Any] = {
        "residual_px": {"rms": round(result.rms_px, 3), "max": round(result.max_px, 3), "n": result.n}}
    if check_norms:
        extra["check_residual_px"] = {"rms": round(float(np.sqrt(np.mean(np.square(check_norms)))), 3),
                                      "max": round(float(np.max(check_norms)), 3), "n": len(check_norms)}
    extra.update(
        house={"path": paths.to_root(house.path) if house.path else None, "sha256": house.sha256},
        eye_space=None if space is None else {"id": space.get("id"), "zone": space.get("zone")},
        notes=pset.notes, open_leaves=pset.open_leaves,
        correspondences=result.correspondence_json() + check_docs,
        solve=dict(result.solve_json(), options=opts.to_json(), guard=guard),
        verticals=va.to_json(), horizon_dy_px=dy_h, snap_warnings=snap_warnings)
    doc = cam.to_json(photo=photo.name, target_distance=result.median_depth_in, **extra)
    eye_to_target = float(np.linalg.norm(np.subtract(doc["target"], doc["eye"])))
    req = make_request(cam, paths.RENDERS / f"{stem}_edges.png", hide_tags=tags, hide_leaves=pset.open_leaves,
                       expect_ir_sha256=house.sha256, target_distance=eye_to_target)
    return Solution(stem, photo, doc, req, guard, log)


def write_solution(sol: Solution, *, house_path: Path | None = paths.HOUSE) -> list[Path]:
    """Write the camera JSON, the render request and the working overlay; returns the paths written."""
    from .compose import compose

    cam_path = paths.CAMERAS / f"{sol.stem}.json"
    cam_path.parent.mkdir(parents=True, exist_ok=True)
    cam_path.write_text(json.dumps(sol.doc, indent=1) + "\n")
    req_path = save_request(sol.request, paths.REQUESTS / f"{sol.stem}.json")
    stats = compose(sol.photo, cam_path, req_path, paths.OVERLAYS / f"{sol.stem}_overlay.png", house_path=house_path)
    return [cam_path, req_path, paths.from_root(stats["overlay"])]


def load_start(path: str | Path) -> dict:
    """A start camera from a camera JSON file, cut to its pose and intrinsics."""
    return camera_start_doc(Camera.from_json(json.loads(Path(path).read_text())))


def _pair(s: str) -> list[float]:
    a, b = s.split(",")
    return [float(a), float(b)]


def build_parser() -> argparse.ArgumentParser:
    d = Options()
    ap = argparse.ArgumentParser(prog="python -m overlay.solve",
                                 description="solve a photo camera from its picks")
    ap.add_argument("photo", help="photo stem (sources/photos/<stem>.jpg, overlay/picks/<stem>.json)")
    ap.add_argument("--house", type=Path, default=paths.HOUSE, help="house.json the references resolve against")
    ap.add_argument("--picks", type=Path, help="picks file (default overlay/picks/<stem>.json)")
    ap.add_argument("--attitude", default=d.attitude, choices=["auto", "free", "zero_pitch", "level"])
    ap.add_argument("--principal", default=d.principal, choices=["free", "dy", "fixed"])
    ap.add_argument("--principal-offset", type=_pair, help="starting (or held) principal offset dx,dy in px")
    ap.add_argument("--principal-prior", type=float, help="soft prior sigma (px) about the principal offset")
    ap.add_argument("--focal", type=float, help="hold the focal length (px)")
    ap.add_argument("--aspect", default=d.aspect, choices=["free", "fixed"], help="solve the pixel aspect fy/fx")
    ap.add_argument("--pixel-aspect", type=float, default=d.pixel_aspect, help="starting (or held) pixel aspect")
    ap.add_argument("--aspect-bounds", type=_pair, default=d.aspect_bounds)
    ap.add_argument("--distortion", default=d.distortion, choices=list(DISTORTIONS),
                    help="radial2 lens: none, held at --k1/--k2, or k1 (and k2) solved under priors")
    ap.add_argument("--k1", type=float, default=d.k1)
    ap.add_argument("--k2", type=float, default=d.k2)
    ap.add_argument("--k1-prior", type=_pair, default=d.k1_prior, help="mean,sigma of a solved k1's prior")
    ap.add_argument("--k2-prior", type=_pair, default=d.k2_prior, help="mean,sigma of a solved k2's prior")
    ap.add_argument("--loss", default=d.loss, choices=["linear", "soft_l1", "huber", "cauchy", "arctan"])
    ap.add_argument("--f-scale", type=float, default=d.f_scale, help="the robust loss's scale, in pick sigmas")
    ap.add_argument("--outlier-threshold", type=float, default=d.outlier_threshold)
    ap.add_argument("--fov-h-bounds", type=_pair, default=d.fov_h_bounds,
                    help="bounds lo,hi (deg) of a free focal length's horizontal field of view")
    ap.add_argument("--start", type=Path, help="a camera JSON to start from (recorded, so a re-solve starts there)")
    ap.add_argument("--no-snap", action="store_true", help="ignore the picks' snap flags")
    ap.add_argument("--snap-radius", type=float, default=d.snap_radius)
    ap.add_argument("--use-lines", action="store_true", help="add the photo's long segments as constraints")
    ap.add_argument("--hide-tags", help="comma-separated tags the render hides (default: by where the eye stands; "
                    "empty for none)")
    ap.add_argument("--dry-run", action="store_true", help="solve and print; write nothing")
    ap.add_argument("--force", action="store_true", help="write the camera even when the write guard refuses it")
    return ap


def options_from_args(a: argparse.Namespace) -> Options:
    return Options(attitude=a.attitude, principal=a.principal, principal_offset=a.principal_offset,
                   principal_prior_px=a.principal_prior, focal=a.focal, aspect=a.aspect, pixel_aspect=a.pixel_aspect,
                   aspect_bounds=list(a.aspect_bounds), distortion=a.distortion, k1=a.k1, k2=a.k2,
                   k1_prior=list(a.k1_prior), k2_prior=list(a.k2_prior), loss=a.loss, f_scale=a.f_scale,
                   outlier_threshold=a.outlier_threshold, fov_h_bounds=list(a.fov_h_bounds),
                   start=None if a.start is None else load_start(a.start),
                   snap=not a.no_snap, snap_radius=a.snap_radius, use_lines=a.use_lines,
                   hide_tags=None if a.hide_tags is None else [t for t in a.hide_tags.split(",") if t])


def main(argv: Sequence[str] | None = None, echo: Callable[[str], Any] = print) -> int:
    a = build_parser().parse_args(argv)
    stem = paths.photo_stem(a.photo)
    try:
        sol = solve_photo(stem, options_from_args(a), house=House.load(a.house), picks=a.picks)
    except (OSError, FeatureError, PicksError, RequestError, ValueError) as e:
        if isinstance(e, CameraError):
            print(f"ERROR: the solve failed: {e}", file=sys.stderr)
            return 1
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    echo("\n".join(sol.log))
    refused = sol.guard["refused"]
    if refused:
        print("REFUSED: " + guard_words(sol.guard) + (" (writing anyway: --force)" if a.force else
                                                   "; nothing written, pass --force to write anyway"), file=sys.stderr)
    sol.doc["solve"]["guard"]["forced"] = bool(refused) and a.force
    if a.dry_run:
        return 3 if refused else 0
    if refused and not a.force:
        return 3
    for p in write_solution(sol, house_path=a.house):
        echo(f"wrote {paths.to_root(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
