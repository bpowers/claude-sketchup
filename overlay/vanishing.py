"""Line segments, vanishing points and the corrected-verticals test.

Coordinates follow camera.py. Vanishing points are homogeneous 3-vectors of unit norm in pixel coordinates, so a
vanishing point at infinity (parallel image lines) is just w = 0.

Before any camera exists:

    segs = detect_segments(image)            # LSD segments, longest first
    va = analyze_verticals(segs, (W, H))     # 'parallel' (corrected verticals), 'converging' or 'undetermined'

and with a rough camera:

    groups = classify_segments(segs, camera) # {'x': indices, 'y': indices, 'z': indices}

which camera.solve_camera(segments=...) takes as extra rows. overlay/solve.py uses all of this for --attitude auto
(parallel verticals mean a level camera), the horizon's principal dy, and --use-lines.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Sequence

import cv2
import numpy as np
from scipy.optimize import least_squares

from .camera import AXIS_VECTORS, Camera, from_cv


def to_gray(image: np.ndarray) -> np.ndarray:
    img = np.asarray(image)
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return img


def detect_segments(image: np.ndarray, *, min_length: float = 20.0) -> np.ndarray:
    """LSD line segments as (N, 4) [u1, v1, u2, v2] in overlay coordinates, longest first."""
    lines = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(to_gray(image))[0]
    if lines is None:
        return np.zeros((0, 4))
    segs = from_cv(np.asarray(lines, dtype=float).reshape(-1, 4))
    segs = segs[segment_lengths(segs) >= min_length]
    return segs[np.argsort(-segment_lengths(segs))]


def segment_lengths(segs: np.ndarray) -> np.ndarray:
    segs = np.asarray(segs, dtype=float).reshape(-1, 4)
    return np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])


def segment_lines(segs: np.ndarray) -> np.ndarray:
    """Homogeneous image lines through each segment, a^2 + b^2 = 1."""
    segs = np.asarray(segs, dtype=float).reshape(-1, 4)
    lines = np.cross(np.column_stack([segs[:, 0], segs[:, 1], np.ones(len(segs))]),
                     np.column_stack([segs[:, 2], segs[:, 3], np.ones(len(segs))]))
    return lines / np.maximum(np.hypot(lines[:, 0], lines[:, 1]), 1e-12)[:, None]


def signed_angular_errors(segs: np.ndarray, vp: np.ndarray) -> np.ndarray:
    """Signed angle (radians, in (-pi/2, pi/2]) between each segment and the line from its midpoint to the vanishing
    point; works for vanishing points at infinity."""
    segs = np.asarray(segs, dtype=float).reshape(-1, 4)
    vp = np.asarray(vp, dtype=float)
    mid = np.column_stack([(segs[:, 0] + segs[:, 2]) / 2.0, (segs[:, 1] + segs[:, 3]) / 2.0])
    d = vp[None, :2] - mid * vp[2]
    s = np.column_stack([segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1]])
    ang = np.arctan2(s[:, 0] * d[:, 1] - s[:, 1] * d[:, 0], s[:, 0] * d[:, 0] + s[:, 1] * d[:, 1])
    ang = np.where(ang > math.pi / 2, ang - math.pi, ang)
    ang = np.where(ang <= -math.pi / 2, ang + math.pi, ang)
    return np.where(np.hypot(d[:, 0], d[:, 1]) < 1e-9, 0.0, ang)


def angular_errors(segs: np.ndarray, vp: np.ndarray) -> np.ndarray:
    return np.abs(signed_angular_errors(segs, vp))


def fit_vanishing_point(segs: np.ndarray, image_size: Sequence[int], *, robust: bool = True) -> np.ndarray:
    """Least-squares vanishing point of a group of segments (unit homogeneous vector): a length-weighted linear start
    in conditioned coordinates, refined on the unit sphere by the length-weighted angular error, so a vanishing point
    at or near infinity needs no special case."""
    segs = np.asarray(segs, dtype=float).reshape(-1, 4)
    if len(segs) < 2:
        raise ValueError("a vanishing point needs at least two segments")
    w = segment_lengths(segs)
    s = 1.0 / max(image_size)
    T = np.array([[s, 0, -s * image_size[0] / 2.0], [0, s, -s * image_size[1] / 2.0], [0, 0, 1.0]])
    Tinv = np.linalg.inv(T)
    lines = segment_lines(segs) @ Tinv                 # lines transform by T^-T
    lines = lines / np.maximum(np.linalg.norm(lines[:, :2], axis=1), 1e-12)[:, None]
    v = np.linalg.eigh((lines * w[:, None]).T @ lines)[1][:, 0]
    if not robust:
        vp = Tinv @ v
        return vp / np.linalg.norm(vp)

    def sph(p):
        return np.array([math.sin(p[0]) * math.cos(p[1]), math.sin(p[0]) * math.sin(p[1]), math.cos(p[0])])

    v = v / np.linalg.norm(v)
    p0 = np.array([math.acos(max(-1.0, min(1.0, v[2]))), math.atan2(v[1], v[0])])
    sw = np.sqrt(w / w.max())
    sol = least_squares(lambda p: sw * signed_angular_errors(segs, Tinv @ sph(p)), p0, loss="soft_l1",
                        f_scale=math.radians(1.0))
    vp = Tinv @ sph(sol.x)
    return vp / np.linalg.norm(vp)


def ransac_vanishing_point(segs: np.ndarray, image_size: Sequence[int], *, threshold_deg: float = 1.5,
                           iterations: int = 800, min_inliers: int = 4,
                           rng: np.random.Generator | None = None) -> tuple[np.ndarray | None, np.ndarray]:
    """The dominant vanishing point by length-weighted RANSAC, refit on its inliers; (None, no inliers) if none."""
    segs = np.asarray(segs, dtype=float).reshape(-1, 4)
    n = len(segs)
    none = (None, np.zeros(n, dtype=bool))
    if n < max(2, min_inliers):
        return none
    rng = rng or np.random.default_rng(0)
    L = segment_lengths(segs)
    lines = segment_lines(segs)
    thr = math.radians(threshold_deg)
    best_score, best = -1.0, None
    for _ in range(iterations):
        i, j = rng.choice(n, 2, replace=False, p=L / L.sum())
        vp = np.cross(lines[i], lines[j])
        nv = np.linalg.norm(vp)
        if nv < 1e-12:
            continue
        inl = angular_errors(segs, vp / nv) < thr
        if L[inl].sum() > best_score:
            best_score, best = L[inl].sum(), inl
    if best is None or best.sum() < min_inliers:
        return none
    vp = fit_vanishing_point(segs[best], image_size)
    mask = angular_errors(segs, vp) < thr
    if mask.sum() < min_inliers:
        return none
    vp = fit_vanishing_point(segs[mask], image_size)
    return vp, angular_errors(segs, vp) < thr


@dataclass
class VanishingCluster:
    vp: list[float]
    n: int
    support_px: float
    indices: list[int] = field(repr=False)

    def point(self) -> tuple[float, float] | None:
        return (self.vp[0] / self.vp[2], self.vp[1] / self.vp[2]) if abs(self.vp[2]) > 1e-9 else None


def detect_vanishing_points(segs: np.ndarray, image_size: Sequence[int], *, count: int = 3,
                            threshold_deg: float = 1.5, min_inliers: int = 6) -> list[VanishingCluster]:
    """Sequential RANSAC: the best-supported vanishing point, its segments removed, repeated."""
    segs = np.asarray(segs, dtype=float).reshape(-1, 4)
    remaining = np.arange(len(segs))
    out = []
    rng = np.random.default_rng(0)
    for _ in range(count):
        vp, inl = ransac_vanishing_point(segs[remaining], image_size, threshold_deg=threshold_deg, rng=rng,
                                         min_inliers=min_inliers)
        if vp is None:
            break
        idx = remaining[inl]
        out.append(VanishingCluster([float(v) for v in vp], int(len(idx)), float(segment_lengths(segs[idx]).sum()),
                                    [int(i) for i in idx]))
        remaining = remaining[~inl]
    return out


def default_focal(image_size: Sequence[int], fov_h_deg: float = 70.0) -> float:
    return (image_size[0] / 2.0) / math.tan(math.radians(fov_h_deg) / 2.0)


def attitude_from_vertical_vp(vp: np.ndarray, image_size: Sequence[int], f: float,
                              principal_offset: Sequence[float] = (0.0, 0.0)) -> tuple[float, float]:
    """(pitch, roll) in degrees implied by the vertical vanishing point for focal length f."""
    cx = image_size[0] / 2.0 + principal_offset[0]
    cy = image_size[1] / 2.0 + principal_offset[1]
    d = np.array([(vp[0] - cx * vp[2]) / f, (vp[1] - cy * vp[2]) / f, vp[2]])
    if d[1] > 0:                      # world up has a negative camera-y component
        d = -d
    d = d / np.linalg.norm(d)
    return math.degrees(math.asin(max(-1.0, min(1.0, d[2])))), math.degrees(math.atan2(d[0], -d[1]))


@dataclass
class VerticalAnalysis:
    state: str                         # 'parallel' | 'converging' | 'undetermined'
    n_segments: int
    pitch_deg: float | None
    pitch_ci95_deg: list[float] | None
    roll_deg: float | None
    f_assumed: float
    note: str
    indices: list[int] = field(default_factory=list, repr=False)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("indices")
        return d


def analyze_verticals(segs: np.ndarray, image_size: Sequence[int], *, f: float | None = None,
                      max_tilt_deg: float = 15.0, threshold_deg: float = 1.0, parallel_tol_deg: float = 0.5,
                      bootstrap: int = 200, min_segments: int = 6) -> VerticalAnalysis:
    """Do the building's verticals converge (camera pitched) or stay parallel (corrected verticals)?

    Finds the vertical vanishing point among near-vertical segments and turns it into the pitch implied for focal
    length f (default a 70 degree horizontal field of view), with a 95% interval from bootstrap resampling.
    'parallel' means the whole interval lies within +/- parallel_tol_deg: the camera is level, and any
    recomposition is a principal offset.
    """
    rng = np.random.default_rng(0)
    segs = np.asarray(segs, dtype=float).reshape(-1, 4)
    f = default_focal(image_size) if f is None else float(f)
    tilt = np.degrees(np.arctan2(np.abs(segs[:, 2] - segs[:, 0]), np.abs(segs[:, 3] - segs[:, 1])))
    cand = np.flatnonzero(tilt < max_tilt_deg)
    empty = VerticalAnalysis("undetermined", 0, None, None, None, f, "too few near-vertical segments")
    if len(cand) < min_segments:
        return empty
    vp, inl = ransac_vanishing_point(segs[cand], image_size, threshold_deg=threshold_deg, rng=rng,
                                     min_inliers=min_segments)
    if vp is None:
        return empty
    idx = cand[inl]
    pitch, roll = attitude_from_vertical_vp(vp, image_size, f)
    boots = []
    for _ in range(bootstrap):
        pick = rng.choice(idx, len(idx), replace=True)
        try:
            boots.append(attitude_from_vertical_vp(fit_vanishing_point(segs[pick], image_size, robust=False),
                                                   image_size, f)[0])
        except ValueError:
            continue
    lo, hi = (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))) if boots else (pitch, pitch)
    lo, hi = min(lo, pitch), max(hi, pitch)
    if -parallel_tol_deg < lo and hi < parallel_tol_deg:
        state, note = "parallel", "verticals are parallel: the camera is level; any recomposition is a principal offset"
    elif (lo > 0 or hi < 0) and abs(pitch) > parallel_tol_deg:
        state, note = "converging", "verticals converge: the camera is pitched"
    else:
        state, note = "undetermined", "the vertical segments do not separate level from pitched"
    return VerticalAnalysis(state, int(len(idx)), pitch, [lo, hi], roll, f, note, [int(i) for i in idx])


def horizon_dy(clusters: Sequence[VanishingCluster], image_size: Sequence[int], roll_deg: float = 0.0,
               max_distance_frac: float = 2.0) -> float | None:
    """The principal dy a level camera's horizon implies: every horizontal vanishing point lies on the horizon, which
    passes through the principal point. Only finite vanishing points within max_distance_frac * max(W, H) of the image
    centre count (a farther one's height is not determined by its segments); the estimate is support-weighted."""
    w, h = image_size
    t = math.tan(math.radians(roll_deg))
    est, wts = [], []
    for c in clusters:
        p = c.point()
        if p is None or math.hypot(p[0] - w / 2.0, p[1] - h / 2.0) > max_distance_frac * max(w, h):
            continue
        est.append(p[1] + (w / 2.0 - p[0]) * t - h / 2.0)
        wts.append(c.support_px)
    return float(np.average(est, weights=wts)) if est else None


def assign_clusters(segs: np.ndarray, clusters: Sequence[VanishingCluster], camera: Camera, *,
                    max_mean_error_deg: float = 25.0, ratio: float = 0.5) -> dict[str, np.ndarray]:
    """Label whole vanishing-point clusters with building axes using a rough camera: each cluster goes to the axis
    whose vanishing point its segments point at best, when that error is under max_mean_error_deg and at most ratio
    times the second-best axis'. The test is relative on purpose: a camera with a wrong focal length or principal point
    moves the vanishing points a long way but still points a family at the right axis, where classifying single
    segments against it would reject exactly the segments that correct it. clusters carry indices into segs."""
    segs = camera.undistort_segments(np.asarray(segs, dtype=float).reshape(-1, 4))
    out: dict[str, list[np.ndarray]] = {"x": [], "y": [], "z": []}
    for c in clusters:
        idx = np.asarray(c.indices, dtype=int)
        if len(idx) < 2:
            continue
        s = segs[idx]
        L = segment_lengths(s)
        errs = {a: float(np.average(angular_errors(s, camera.vanishing_point(AXIS_VECTORS[a])), weights=L))
                for a in "xyz"}
        best, second = sorted(errs, key=errs.get)[:2]
        if math.degrees(errs[best]) <= max_mean_error_deg and errs[best] <= ratio * errs[second]:
            out[best].append(idx)
    return {a: (np.unique(np.concatenate(v)) if v else np.zeros(0, dtype=int)) for a, v in out.items()}


def classify_segments(segs: np.ndarray, camera: Camera, *, threshold_deg: float = 1.5,
                      margin: float = 2.0) -> dict[str, np.ndarray]:
    """Assign single segments to the building axes by a camera's vanishing points: the best axis within
    threshold_deg, with the second best at least margin times worse."""
    segs = camera.undistort_segments(np.asarray(segs, dtype=float).reshape(-1, 4))
    errs = np.vstack([angular_errors(segs, camera.vanishing_point(AXIS_VECTORS[a])) for a in "xyz"])
    order = np.argsort(errs, axis=0)
    cols = np.arange(len(segs))
    e1, e2 = errs[order[0], cols], errs[order[1], cols]
    ok = (e1 < math.radians(threshold_deg)) & (e2 > margin * e1)
    return {a: np.flatnonzero(ok & (order[0] == i)) for i, a in enumerate("xyz")}
