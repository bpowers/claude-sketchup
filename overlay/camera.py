"""The photo camera: projection, rays, the camera JSON, and solving a camera from picks.

CONVENTIONS (every module in overlay/ uses these)

World frame
    The house frame of docs/IR.md, in inches: +x and +y the plan axes the house chose, +z up, z = 0 the main
    floor's finish floor.

Image coordinates
    Continuous pixels with the origin at the top-left CORNER of the top-left pixel, u to the right, v down. Pixel
    (i, j) has its centre at (i + 0.5, j + 0.5), and the image centre is (W/2, H/2). OpenCV puts pixel centres on
    integers, so positions from OpenCV get +0.5 (``from_cv``) and positions handed to it get -0.5 (``to_cv``).

Camera
    OpenCV's camera frame: x right, y down, z forward along the optical axis. ``R`` maps world vectors into that
    frame (its rows are the camera's right, down and forward vectors in world coordinates), and a world point X has
    camera coordinates R (X - eye). A pinhole projects

        u = f xc / zc + W/2 + dx,     v = a f yc / zc + H/2 + dy

    with ``f`` the horizontal focal length in pixels, ``a`` the pixel aspect fy / fx (1 is square pixels) and
    (dx, dy) the principal offset, how far the principal point sits from the image centre. There is no skew.
    Listing photographers correct verticals: the camera is then level, and the recomposition is a principal offset
    (usually dy) and often a vertical stretch, which is the pixel aspect.

Lens distortion
    ``distortion`` = (k1, k2), the "radial2" model in OpenCV's convention on the normalised pinhole coordinates
    x = xc / zc, y = yc / zc:

        r^2 = x^2 + y^2,    s = 1 + k1 r^2 + k2 r^4,    u = f (x s) + cx,    v = a f (y s) + cy

    Picks are read off the photo as it is, distortion included, so ``project`` distorts and ``ray`` undistorts
    first. A 3D line projects to a curve; ``line_foot`` measures a pick's distance from that curve. The radial map
    r -> r s(r) must stay monotonic over the frame; ``lens_problem`` names a lens that folds over inside it.

Attitude angles (reporting, and the level / zero-pitch solves)
    yaw    heading of the optical axis in plan, degrees counterclockwise from +x (so +y is 90)
    pitch  elevation of the optical axis above the horizontal, degrees
    roll   rotation about the optical axis, degrees, positive when the camera turns counterclockwise seen from behind

Camera JSON (overlay/cameras/<stem>.json)
    {"photo", "image_size": [W, H], "eye": [x, y, z], "target": [x, y, z], "up": [x, y, z], "fov_v_deg",
     "principal_offset_px": [dx, dy], "pixel_aspect", "distortion"?: {"model": "radial2", "k1", "k2"}, ...}
    ``fov_v_deg`` is 2 atan((H / 2) / f), from the horizontal focal length: the vertical field of view of the
    square-pixel camera SketchUp renders. ``up`` is written exactly (orthogonal to the view) and read as a hint, as
    SketchUp's Camera.new(eye, target, up) reads it. ``distortion`` is written only when non-zero. overlay/solve.py
    adds the solve's record (docs/PHOTOS.md lists the keys).

Picks and residuals
    A ``Correspondence`` ties a pixel to a model point, or to a model line: one model coordinate None (a line along
    that axis, such as a vertical edge [x, y, None]), or a full point with a ``direction`` (a line along no axis, a
    side of a skewed wall). A residual is projected minus observed, in pixels; for a line it is the distance from the
    pick to the projected line (a curve under distortion).

Solving (``solve_camera``)
    At least six picks, four of them full points. The starts are the normalised DLT (six or more full points off one
    plane), SQPnP over a grid of fields of view, and an optional given camera; each is refined by robust least
    squares (scipy, soft_l1 by default) on pixel residuals over sigma, with prior rows on a solved lens, and the
    lowest cost wins. Optional segments (overlay/vanishing.py) add rows that make each segment point at its building
    axis' vanishing point. The uncertainty comes from the Jacobian at the solution, scaled by the residuals.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np
from scipy.linalg import rq
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

MIN_CORRESPONDENCES = 6
MIN_FULL_POINTS = 4
AXIS_VECTORS = {"x": np.array([1.0, 0.0, 0.0]), "y": np.array([0.0, 1.0, 0.0]), "z": np.array([0.0, 0.0, 1.0])}
ATTITUDES = ("free", "zero_pitch", "level")
PRINCIPALS = ("free", "dy", "fixed")
FOCALS = ("free", "fixed")
ASPECTS = ("free", "fixed")
# fy / fx of a perspective-corrected photo lies well inside this; a free solve that runs to a bound is refused
ASPECT_BOUNDS = (0.8, 1.25)
DISTORTION_MODEL = "radial2"
# none: k1 = k2 = 0; fixed: held at the given k1, k2; k1: k1 solved, k2 held; k1k2: both solved
DISTORTIONS = ("none", "fixed", "k1", "k1k2")
# (mean, sigma) priors on solved radial coefficients: listing photos are usually lens-corrected, so near zero
K1_PRIOR = (0.0, 0.15)
K2_PRIOR = (0.0, 0.1)
K_BOUNDS = (-1.0, 1.0)
# Where the radial map's slope d(r s) / dr falls below this at the frame corner, the pinhole canvas is magnified
# more than fourfold there: the lens model is folding over inside the photo, which no real lens does.
LENS_MIN_SLOPE = 0.25
# the keys of a start camera: a camera JSON's pose and intrinsics
START_CAMERA_KEYS = ("image_size", "eye", "target", "up", "fov_v_deg", "principal_offset_px", "pixel_aspect",
                     "distortion")


class CameraError(ValueError):
    """Invalid camera input, or a solve whose result cannot be used at all."""


def from_cv(uv: np.ndarray) -> np.ndarray:
    """OpenCV pixel positions (centres on integers) to overlay image coordinates."""
    return np.asarray(uv, dtype=float) + 0.5


def to_cv(uv: np.ndarray) -> np.ndarray:
    """Overlay image coordinates to OpenCV pixel positions."""
    return np.asarray(uv, dtype=float) - 0.5


# -------------------------------------------------------------------- the lens


def radial_fold_radius(k1: float, k2: float) -> float:
    """The smallest normalised pinhole radius at which r (1 + k1 r^2 + k2 r^4) stops increasing, or inf."""
    if k2 == 0.0:
        return math.inf if k1 >= 0.0 else math.sqrt(-1.0 / (3.0 * k1))
    disc = 9.0 * k1 * k1 - 20.0 * k2              # the slope 1 + 3 k1 s + 5 k2 s^2 in s = r^2
    if disc < 0.0:
        return math.inf
    q = math.sqrt(disc)
    roots = [s for s in ((-3.0 * k1 - q) / (10.0 * k2), (-3.0 * k1 + q) / (10.0 * k2)) if s > 0.0]
    return math.sqrt(min(roots)) if roots else math.inf


def radial_distort(r, k1: float, k2: float):
    """r (1 + k1 r^2 + k2 r^4): the distorted normalised radius of a pinhole radius."""
    r2 = np.square(r)
    return r * (1.0 + k1 * r2 + k2 * r2 * r2)


def radial_slope(r, k1: float, k2: float):
    """d/dr of radial_distort."""
    r2 = np.square(r)
    return 1.0 + 3.0 * k1 * r2 + 5.0 * k2 * r2 * r2


def radial_undistort(rd, k1: float, k2: float) -> np.ndarray:
    """The pinhole radius r >= 0 on the monotonic branch with radial_distort(r) = rd, by bracketed Newton. A radius
    the lens cannot produce (past the fold) gives the fold radius."""
    rd = np.asarray(rd, dtype=float)
    if k1 == 0.0 and k2 == 0.0:
        return rd.copy()
    rf = radial_fold_radius(k1, k2)
    lo = np.zeros_like(rd)
    if math.isfinite(rf):
        hi = np.full_like(rd, rf)
    else:
        hi = np.maximum(rd, 1e-12)
        for _ in range(200):
            short = radial_distort(hi, k1, k2) < rd
            if not np.any(short):
                break
            hi = np.where(short, 2.0 * hi + 1e-3, hi)
    r = np.clip(rd, lo, hi)
    scale = max(1.0, float(np.max(rd, initial=0.0)))
    for _ in range(80):
        g = radial_distort(r, k1, k2) - rd
        lo = np.where(g <= 0.0, r, lo)
        hi = np.where(g >= 0.0, r, hi)
        dg = radial_slope(r, k1, k2)
        with np.errstate(divide="ignore", invalid="ignore"):
            rn = r - g / dg
        bad = ~np.isfinite(rn) | (dg <= 0.0) | (rn < lo) | (rn > hi)
        rn = np.where(bad, 0.5 * (lo + hi), rn)
        if float(np.max(np.abs(rn - r), initial=0.0)) <= 1e-13 * scale:
            return rn
        r = rn
    return r


def _finite_pair(v: Any) -> bool:
    return (isinstance(v, (tuple, list, np.ndarray)) and len(v) == 2 and
            all(not isinstance(x, bool) and isinstance(x, (int, float, np.floating)) and math.isfinite(x) for x in v))


def parse_distortion(d: Any, where: str = "distortion") -> tuple[float, float]:
    """(k1, k2) of a distortion object {"model": "radial2", "k1": k1, "k2": k2}; k2 defaults to 0."""
    if not isinstance(d, Mapping):
        raise CameraError(f"{where} must be an object {{\"model\": \"radial2\", \"k1\": k1, \"k2\": k2}}, got {d!r}")
    unknown = set(d) - {"model", "k1", "k2"}
    if unknown:
        raise CameraError(f"{where} has unknown keys {sorted(unknown)}")
    if d.get("model") != DISTORTION_MODEL:
        raise CameraError(f"{where} model must be {DISTORTION_MODEL!r}, got {d.get('model')!r}")
    out = []
    for key in ("k1", "k2"):
        v = d.get(key, 0.0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise CameraError(f"{where} {key} must be a finite number, got {v!r}")
        out.append(float(v))
    return out[0], out[1]


# -------------------------------------------------------------------- rotations


def rotation_from_euler(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """World-to-camera rotation for the yaw / pitch / roll convention in the module doc."""
    psi, theta, phi = np.radians([yaw_deg, pitch_deg, roll_deg])
    fwd = np.array([math.cos(theta) * math.cos(psi), math.cos(theta) * math.sin(psi), math.sin(theta)])
    r0 = np.array([math.sin(psi), -math.cos(psi), 0.0])
    u0 = np.cross(r0, fwd)
    right = math.cos(phi) * r0 + math.sin(phi) * u0
    up = -math.sin(phi) * r0 + math.cos(phi) * u0
    return np.vstack([right, -up, fwd])


def euler_from_rotation(R: np.ndarray) -> tuple[float, float, float]:
    """Inverse of rotation_from_euler, in degrees. Looking straight up or down, roll is reported as 0."""
    R = np.asarray(R, dtype=float)
    fwd, right = R[2], R[0]
    theta = math.asin(max(-1.0, min(1.0, fwd[2])))
    if math.hypot(fwd[0], fwd[1]) < 1e-9:
        return math.degrees(math.atan2(right[0], -right[1])), math.degrees(theta), 0.0
    psi = math.atan2(fwd[1], fwd[0])
    r0 = np.array([math.sin(psi), -math.cos(psi), 0.0])
    u0 = np.cross(r0, fwd)
    phi = math.atan2(float(right @ u0), float(right @ r0))
    return math.degrees(psi), math.degrees(theta), math.degrees(phi)


def look_at_rotation(eye: Sequence[float], target: Sequence[float], up: Sequence[float]) -> np.ndarray:
    """World-to-camera rotation looking from eye at target, with up as a hint."""
    eye = np.asarray(eye, dtype=float)
    fwd = np.asarray(target, dtype=float) - eye
    n = np.linalg.norm(fwd)
    if not np.isfinite(n) or n < 1e-9:
        raise CameraError("camera target coincides with eye")
    fwd = fwd / n
    right = np.cross(fwd, np.asarray(up, dtype=float))
    rn = np.linalg.norm(right)
    if not np.isfinite(rn) or rn < 1e-9:
        raise CameraError("camera up vector is parallel to the view direction")
    right /= rn
    return np.vstack([right, -np.cross(right, fwd), fwd])


def _check_rotation(R: np.ndarray) -> np.ndarray:
    R = np.asarray(R, dtype=float).reshape(3, 3)
    if not np.all(np.isfinite(R)):
        raise CameraError("rotation matrix has non-finite entries")
    if not np.allclose(R @ R.T, np.eye(3), atol=1e-6) or np.linalg.det(R) < 0:
        raise CameraError("R is not a proper rotation matrix")
    return R


# ----------------------------------------------------------------------- camera


@dataclass(frozen=True)
class Camera:
    """A pinhole camera, with pixel aspect and radial lens, for one image. See the module doc for conventions."""

    image_size: tuple[int, int]
    f: float
    R: np.ndarray
    eye: np.ndarray
    principal_offset: tuple[float, float] = (0.0, 0.0)
    pixel_aspect: float = 1.0
    distortion: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        if not _finite_pair(self.distortion):
            raise CameraError(f"distortion must be two finite numbers (k1, k2), got {self.distortion!r}")
        object.__setattr__(self, "distortion", (float(self.distortion[0]), float(self.distortion[1])))
        w, h = self.image_size
        if int(w) != w or int(h) != h or w <= 0 or h <= 0:
            raise CameraError(f"image_size must be positive integers, got {self.image_size}")
        object.__setattr__(self, "image_size", (int(w), int(h)))
        if not (np.isfinite(self.f) and self.f > 0):
            raise CameraError(f"focal length must be positive and finite, got {self.f}")
        object.__setattr__(self, "f", float(self.f))
        object.__setattr__(self, "R", _check_rotation(self.R))
        eye = np.asarray(self.eye, dtype=float).reshape(3)
        if not np.all(np.isfinite(eye)):
            raise CameraError("eye has non-finite coordinates")
        object.__setattr__(self, "eye", eye)
        dx, dy = (float(v) for v in self.principal_offset)
        if not (np.isfinite(dx) and np.isfinite(dy)):
            raise CameraError("principal offset must be finite")
        object.__setattr__(self, "principal_offset", (dx, dy))
        a = self.pixel_aspect
        if isinstance(a, bool) or not isinstance(a, (int, float, np.floating)) or not (np.isfinite(a) and a > 0):
            raise CameraError(f"pixel_aspect must be a positive finite number, got {a!r}")
        object.__setattr__(self, "pixel_aspect", float(a))

    # ------------------------------------------------------------ constructors

    @classmethod
    def look_at(cls, image_size: Sequence[int], eye: Sequence[float], target: Sequence[float],
                up: Sequence[float] = (0.0, 0.0, 1.0), *, fov_v_deg: float | None = None, f: float | None = None,
                principal_offset: Sequence[float] = (0.0, 0.0), pixel_aspect: float = 1.0,
                distortion: Sequence[float] = (0.0, 0.0)) -> "Camera":
        """A camera from eye / target / up and either fov_v_deg (from the horizontal focal length, as in the camera
        JSON) or f in pixels."""
        if (fov_v_deg is None) == (f is None):
            raise CameraError("give exactly one of fov_v_deg and f")
        if f is None:
            if not (0.0 < fov_v_deg < 180.0):
                raise CameraError(f"fov_v_deg must be in (0, 180), got {fov_v_deg}")
            f = (image_size[1] / 2.0) / math.tan(math.radians(fov_v_deg) / 2.0)
        return cls(tuple(image_size), f, look_at_rotation(eye, target, up), np.asarray(eye, float),
                   tuple(principal_offset), pixel_aspect, tuple(distortion))

    @classmethod
    def from_euler(cls, image_size: Sequence[int], f: float, eye: Sequence[float], yaw_deg: float,
                   pitch_deg: float = 0.0, roll_deg: float = 0.0, principal_offset: Sequence[float] = (0.0, 0.0),
                   pixel_aspect: float = 1.0, distortion: Sequence[float] = (0.0, 0.0)) -> "Camera":
        return cls(tuple(image_size), f, rotation_from_euler(yaw_deg, pitch_deg, roll_deg), np.asarray(eye, float),
                   tuple(principal_offset), pixel_aspect, tuple(distortion))

    def replace(self, **kw: Any) -> "Camera":
        d = dict(image_size=self.image_size, f=self.f, R=self.R, eye=self.eye, principal_offset=self.principal_offset,
                 pixel_aspect=self.pixel_aspect, distortion=self.distortion)
        d.update(kw)
        return Camera(**d)

    def centered(self, width: int, height: int) -> "Camera":
        """Same eye, attitude and horizontal focal length on a width x height canvas, principal point centred,
        square pixels, no lens: the camera SketchUp renders for this one (overlay/request.py)."""
        return Camera((width, height), self.f, self.R, self.eye)

    def pinhole(self) -> "Camera":
        """The same camera without lens distortion."""
        return self.replace(distortion=(0.0, 0.0))

    # -------------------------------------------------------------- properties

    @property
    def width(self) -> int:
        return self.image_size[0]

    @property
    def height(self) -> int:
        return self.image_size[1]

    @property
    def cx(self) -> float:
        return self.width / 2.0 + self.principal_offset[0]

    @property
    def cy(self) -> float:
        return self.height / 2.0 + self.principal_offset[1]

    @property
    def fy(self) -> float:
        """Vertical focal length in pixels: pixel_aspect * f."""
        return self.pixel_aspect * self.f

    @property
    def k1(self) -> float:
        return self.distortion[0]

    @property
    def k2(self) -> float:
        return self.distortion[1]

    @property
    def distorted(self) -> bool:
        return self.distortion != (0.0, 0.0)

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.f, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]])

    @property
    def P(self) -> np.ndarray:
        """3x4 pinhole projection matrix in overlay image coordinates."""
        return self.K @ np.hstack([self.R, (-self.R @ self.eye)[:, None]])

    @property
    def right(self) -> np.ndarray:
        return self.R[0].copy()

    @property
    def up(self) -> np.ndarray:
        return -self.R[1]

    @property
    def forward(self) -> np.ndarray:
        return self.R[2].copy()

    @property
    def fov_v_deg(self) -> float:
        """2 atan((H / 2) / f): the square-pixel camera's vertical field of view, as the camera JSON records it."""
        return math.degrees(2.0 * math.atan((self.height / 2.0) / self.f))

    @property
    def fov_h_deg(self) -> float:
        return math.degrees(2.0 * math.atan((self.width / 2.0) / self.f))

    def euler_deg(self) -> tuple[float, float, float]:
        return euler_from_rotation(self.R)

    # -------------------------------------------------------------- projection

    def to_camera_frame(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float).reshape(-1, 3)
        return (X - self.eye) @ self.R.T

    def project(self, X: np.ndarray, *, return_depth: bool = False):
        """Photo pixels of world points, lens included: (N, 2), plus (N,) depths if asked. A point at or behind the
        camera gives a meaningless pixel; check the depth."""
        X = np.asarray(X, dtype=float)
        single = X.ndim == 1
        Xc = self.to_camera_frame(X)
        z = Xc[:, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            uv = self._project_normalised(Xc[:, :2] / z[:, None])
        if single:
            uv, z = uv[0], z[0]
        return (uv, z) if return_depth else uv

    def _project_normalised(self, xy: np.ndarray) -> np.ndarray:
        """Photo pixels of (N, 2) normalised pinhole coordinates xc / zc, yc / zc: the lens, then the focal
        lengths and the principal point."""
        r2 = np.sum(xy * xy, axis=1)
        s = 1.0 + self.k1 * r2 + self.k2 * r2 * r2
        return np.array([self.f, self.fy]) * xy * s[:, None] + np.array([self.cx, self.cy])

    def vanishing_point(self, direction: Sequence[float]) -> np.ndarray:
        """Homogeneous pinhole image point (unit norm) where lines of this world direction converge. Under lens
        distortion it holds for undistorted coordinates (undistort_px)."""
        v = self.K @ (self.R @ np.asarray(direction, dtype=float))
        return v / np.linalg.norm(v)

    def project_line(self, X0: np.ndarray, direction: Sequence[float]) -> np.ndarray:
        """Pinhole image lines (a, b, c), a^2 + b^2 = 1, of the 3D lines X0 + t direction. Under lens distortion
        the projection is a curve and these lines hold for undistorted coordinates; see line_foot."""
        X0 = np.asarray(X0, dtype=float).reshape(-1, 3)
        P = self.P
        p0 = np.hstack([X0, np.ones((len(X0), 1))]) @ P.T
        vp = P @ np.append(np.asarray(direction, dtype=float), 0.0)
        lines = np.cross(p0, vp[None, :])
        return lines / np.maximum(np.hypot(lines[:, 0], lines[:, 1]), 1e-300)[:, None]

    def ray(self, uv: np.ndarray) -> np.ndarray:
        """Unit world directions of the rays through photo points (undistorted first)."""
        uv = self.undistort_px(np.asarray(uv, dtype=float).reshape(-1, 2))
        d = np.column_stack([(uv[:, 0] - self.cx) / self.f, (uv[:, 1] - self.cy) / self.fy, np.ones(len(uv))])
        d = d @ self.R
        return d / np.linalg.norm(d, axis=1, keepdims=True)

    # ------------------------------------------------------------------- lens

    def distort_px(self, uv: np.ndarray) -> np.ndarray:
        """Pinhole pixels of this camera to photo pixels (lens applied)."""
        uv = np.asarray(uv, dtype=float)
        single = uv.ndim == 1
        uv = uv.reshape(-1, 2)
        out = uv.copy()
        if self.distorted:
            c, fl = np.array([self.cx, self.cy]), np.array([self.f, self.fy])
            xy = (uv - c) / fl
            r2 = np.sum(xy * xy, axis=1)
            out = xy * (1.0 + self.k1 * r2 + self.k2 * r2 * r2)[:, None] * fl + c
        return out[0] if single else out

    def undistort_px(self, uv: np.ndarray) -> np.ndarray:
        """Photo pixels to this camera's pinhole pixels (lens removed)."""
        uv = np.asarray(uv, dtype=float)
        single = uv.ndim == 1
        uv = uv.reshape(-1, 2)
        out = uv.copy()
        if self.distorted:
            c, fl = np.array([self.cx, self.cy]), np.array([self.f, self.fy])
            xy = (uv - c) / fl
            rd = np.hypot(xy[:, 0], xy[:, 1])
            r = radial_undistort(rd, self.k1, self.k2)
            with np.errstate(divide="ignore", invalid="ignore"):
                s = np.where(rd > 0.0, r / rd, 1.0)
            out = xy * s[:, None] * fl + c
        return out[0] if single else out

    def distortion_jacobian_px(self, uv: np.ndarray) -> np.ndarray:
        """(N, 2, 2) derivatives of distort_px at pinhole pixels: d(photo u, v) / d(pinhole u, v)."""
        uv = np.asarray(uv, dtype=float).reshape(-1, 2)
        J = np.zeros((len(uv), 2, 2))
        xy = (uv - np.array([self.cx, self.cy])) / np.array([self.f, self.fy])
        x, y = xy[:, 0], xy[:, 1]
        r2 = x * x + y * y
        s = 1.0 + self.k1 * r2 + self.k2 * r2 * r2
        ds = self.k1 + 2.0 * self.k2 * r2                      # ds / d(r^2)
        J[:, 0, 0] = s + 2.0 * x * x * ds
        J[:, 1, 1] = s + 2.0 * y * y * ds
        J[:, 0, 1] = 2.0 * x * y * ds * self.f / self.fy
        J[:, 1, 0] = 2.0 * x * y * ds * self.fy / self.f
        return J

    def undistort_segments(self, segs: np.ndarray) -> np.ndarray:
        """(N, 4) photo segments [u1, v1, u2, v2] with both endpoints undistorted."""
        segs = np.asarray(segs, dtype=float).reshape(-1, 4)
        if not self.distorted or not len(segs):
            return segs.copy()
        return np.hstack([self.undistort_px(segs[:, :2]), self.undistort_px(segs[:, 2:])])

    def pinhole_frame_bounds(self, samples: int = 64) -> tuple[float, float, float, float]:
        """(u_min, u_max, v_min, v_max) in pinhole pixels of the photo frame undistorted: the region a pinhole
        render must cover so the whole photo can be warped out of it."""
        w, h = self.image_size
        if not self.distorted:
            return 0.0, float(w), 0.0, float(h)
        t = np.linspace(0.0, 1.0, max(2, int(samples)))
        edge = np.vstack([np.column_stack([t * w, np.zeros_like(t)]), np.column_stack([t * w, np.full_like(t, h)]),
                          np.column_stack([np.zeros_like(t), t * h]), np.column_stack([np.full_like(t, w), t * h])])
        p = self.undistort_px(edge)
        return float(p[:, 0].min()), float(p[:, 0].max()), float(p[:, 1].min()), float(p[:, 1].max())

    def lens_problem(self) -> str | None:
        """Why this lens cannot describe the photo (its radial map folds over, or nearly, inside the frame), or
        None."""
        if not self.distorted:
            return None
        w, h = self.image_size
        corners = np.array([[0.0, 0.0], [w, 0.0], [0.0, h], [w, h]])
        xy = (corners - np.array([self.cx, self.cy])) / np.array([self.f, self.fy])
        rd = float(np.hypot(xy[:, 0], xy[:, 1]).max())
        k1, k2 = self.distortion
        rf = radial_fold_radius(k1, k2)
        if math.isfinite(rf) and rd >= float(radial_distort(rf, k1, k2)) * (1.0 - 1e-9):
            return (f"radial2 lens k1 {k1:.4g}, k2 {k2:.4g} folds over inside the photo: no pinhole radius reaches "
                    f"the frame corner's distorted radius {rd:.3f}")
        slope = float(radial_slope(float(radial_undistort(np.array([rd]), k1, k2)[0]), k1, k2))
        if slope < LENS_MIN_SLOPE:
            return (f"radial2 lens k1 {k1:.4g}, k2 {k2:.4g} nearly folds over at the frame corner: d(r s)/dr is "
                    f"{slope:.3f} there (limit {LENS_MIN_SLOPE:g})")
        return None

    def line_foot(self, px: np.ndarray, X0: np.ndarray,
                  direction: Sequence[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """For photo points px and the 3D lines X0 + t direction: the foot on each projected line (a curve under
        distortion), the signed distance of px from it and the unit normal it is measured along, as (N, 2), (N,),
        (N, 2). With distortion the pick is undistorted, footed on the pinhole line and the foot distorted back; the
        distance is taken along the curve's normal there, so it is zero exactly on the curve and smooth in every
        camera parameter."""
        px = np.asarray(px, dtype=float).reshape(-1, 2)
        lines = self.project_line(X0, direction)
        n_u = lines[:, :2]
        if not self.distorted:
            d = np.sum(lines * np.column_stack([px, np.ones(len(px))]), axis=1)
            return px - d[:, None] * n_u, d, n_u.copy()
        p_u = self.undistort_px(px)
        d_u = np.sum(lines * np.column_stack([p_u, np.ones(len(p_u))]), axis=1)
        q_u = p_u - d_u[:, None] * n_u
        q_d = self.distort_px(q_u)
        J = self.distortion_jacobian_px(q_u)
        t_d = np.einsum("nij,nj->ni", J, np.column_stack([-n_u[:, 1], n_u[:, 0]]))
        n_d = np.column_stack([t_d[:, 1], -t_d[:, 0]])
        n_d = n_d / np.maximum(np.linalg.norm(n_d, axis=1), 1e-300)[:, None]
        orient = np.sign(np.sum(n_d * np.einsum("nij,nj->ni", J, n_u), axis=1))
        n_d = n_d * np.where(orient == 0.0, 1.0, orient)[:, None]
        return q_d, np.sum((px - q_d) * n_d, axis=1), n_d

    # -------------------------------------------------------------------- JSON

    def to_json(self, *, photo: str | None = None, target_distance: float | None = None, **extra: Any) -> dict:
        """The camera JSON; extra keyword arguments are appended as keys."""
        d = 120.0 if target_distance is None else float(target_distance)
        if not (np.isfinite(d) and d > 0):
            raise CameraError(f"target_distance must be positive, got {target_distance}")
        out: dict[str, Any] = {} if photo is None else {"photo": photo}
        out["image_size"] = [self.width, self.height]
        out["eye"] = [round(float(v), 6) for v in self.eye]
        out["target"] = [round(float(v), 6) for v in self.eye + d * self.forward]
        out["up"] = [round(float(v), 12) for v in self.up]
        out["fov_v_deg"] = round(self.fov_v_deg, 12)
        out["principal_offset_px"] = [round(v, 6) for v in self.principal_offset]
        out["pixel_aspect"] = round(self.pixel_aspect, 9)
        if self.distorted:
            out["distortion"] = lens_json(self)
        out.update(extra)
        return out

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Camera":
        for key in ("image_size", "eye", "target", "fov_v_deg"):
            if key not in d:
                raise CameraError(f"camera JSON is missing '{key}'")
        size = d["image_size"]
        if len(size) != 2:
            raise CameraError(f"image_size must be [width, height], got {size}")
        dist = d.get("distortion")
        return cls.look_at((int(size[0]), int(size[1])), d["eye"], d["target"], d.get("up", [0.0, 0.0, 1.0]),
                           fov_v_deg=float(d["fov_v_deg"]), principal_offset=d.get("principal_offset_px", [0.0, 0.0]),
                           pixel_aspect=d.get("pixel_aspect") or 1.0,
                           distortion=(0.0, 0.0) if dist is None else parse_distortion(dist, "camera JSON distortion"))

    def save(self, path: str | Path, **kw: Any) -> dict:
        doc = self.to_json(**kw)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(doc, indent=1) + "\n")
        return doc

    @classmethod
    def load(cls, path: str | Path) -> "Camera":
        return cls.from_json(json.loads(Path(path).read_text()))


def lens_json(camera: Camera) -> dict:
    """A camera's distortion object."""
    return {"model": DISTORTION_MODEL, "k1": round(camera.k1, 12), "k2": round(camera.k2, 12)}


# -------------------------------------------------------------- correspondences


@dataclass(frozen=True)
class Correspondence:
    """One pick: a pixel and the model point or line it shows.

    ``model`` may leave one coordinate None: the pixel then lies on the model line through the known coordinates
    along that axis ([300.0, 120.0, None] is a vertical edge at that plan position). A full point with a
    ``direction`` is a line along that direction through it.
    """

    label: str
    px: tuple[float, float]
    model: tuple[float | None, float | None, float | None]
    sigma_px: float = 2.0
    snap: bool = False
    px_approx: tuple[float, float] | None = None
    snap_quality: float | None = None
    note: str | None = None
    # facts about the pick the solve does not use but the camera JSON records (the reference, its role, ...)
    extra: Mapping[str, Any] | None = field(default=None, compare=False)
    direction: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise CameraError(f"correspondence label must be a non-empty string, got {self.label!r}")
        px = tuple(float(v) for v in self.px)
        if len(px) != 2 or not all(np.isfinite(px)):
            raise CameraError(f"{self.label}: px must be two finite numbers, got {self.px}")
        object.__setattr__(self, "px", px)
        if len(self.model) != 3:
            raise CameraError(f"{self.label}: model must have three coordinates, got {self.model}")
        model = tuple(None if v is None else float(v) for v in self.model)
        unknown = sum(v is None for v in model)
        if unknown > 1:
            raise CameraError(f"{self.label}: at most one model coordinate may be unknown; a pixel on a model plane "
                              "constrains nothing")
        if not all(v is None or np.isfinite(v) for v in model):
            raise CameraError(f"{self.label}: model coordinates must be finite")
        object.__setattr__(self, "model", model)
        if not (np.isfinite(self.sigma_px) and self.sigma_px > 0):
            raise CameraError(f"{self.label}: sigma_px must be positive, got {self.sigma_px}")
        if self.direction is not None:
            d = np.asarray(self.direction, dtype=float).reshape(-1)
            if d.shape != (3,) or not np.all(np.isfinite(d)) or not np.linalg.norm(d) > 1e-9:
                raise CameraError(f"{self.label}: direction must be three finite numbers, not all zero")
            if unknown:
                raise CameraError(f"{self.label}: a line with a direction runs through a full model point")
            object.__setattr__(self, "direction", tuple(float(v) for v in d / np.linalg.norm(d)))

    @property
    def free_axis(self) -> int | None:
        """The coordinate a line leaves free (for a line with a direction, the axis it runs nearest); None for a
        point."""
        if self.direction is not None:
            return int(np.argmax(np.abs(self.direction)))
        return next((i for i, v in enumerate(self.model) if v is None), None)

    @property
    def is_point(self) -> bool:
        return self.free_axis is None

    @property
    def role(self) -> str:
        return (self.extra or {}).get("role", "solve")


def line_through(model: Sequence[float | None], direction: Sequence[float] | None,
                 eye: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """(X0, d) of a line pick for drawing and footing: an axis line takes the eye's coordinate on its free axis, a
    line with a direction its point nearest the eye."""
    eye = np.asarray(eye, dtype=float)
    if direction is None:
        k = next(i for i, v in enumerate(model) if v is None)
        return np.array([eye[i] if v is None else float(v) for i, v in enumerate(model)]), AXIS_VECTORS["xyz"[k]].copy()
    d = np.asarray(direction, dtype=float)
    X = np.asarray(model, dtype=float)
    return X + float((eye - X) @ d) * d, d


def target_point(camera: Camera, px: Sequence[float], model: Sequence[float | None],
                 direction: Sequence[float] | None = None) -> np.ndarray:
    """The model point a pick shows: the point itself, or for a line the point of the line nearest the pick's ray."""
    if direction is None and all(v is not None for v in model):
        return np.asarray(model, dtype=float)
    if direction is None:
        k = next(i for i, v in enumerate(model) if v is None)
        X0 = np.array([0.0 if v is None else float(v) for v in model])
        e = AXIS_VECTORS["xyz"[k]]
    else:
        X0, e = np.asarray(model, dtype=float), np.asarray(direction, dtype=float)
    r = camera.ray(np.asarray(px, dtype=float)[None, :])[0]
    w0 = camera.eye - X0
    b, dr, de = float(r @ e), float(r @ w0), float(e @ w0)
    denom = 1.0 - b * b
    t = de if denom < 1e-9 else (de - b * dr) / denom
    return X0 + t * e


# ---------------------------------------------------------------------- starts


def _normalizer(pts: np.ndarray) -> np.ndarray:
    """Hartley normalisation: centroid to the origin, mean distance sqrt(dim)."""
    dim = pts.shape[1]
    c = pts.mean(axis=0)
    s = math.sqrt(dim) / max(np.linalg.norm(pts - c, axis=1).mean(), 1e-12)
    T = np.eye(dim + 1)
    T[:dim, :dim] *= s
    T[:dim, dim] = -s * c
    return T


def dlt_projection(X: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Normalised DLT estimate of the 3x4 projection matrix from at least 6 points."""
    X = np.asarray(X, dtype=float).reshape(-1, 3)
    uv = np.asarray(uv, dtype=float).reshape(-1, 2)
    if len(X) < 6 or len(X) != len(uv):
        raise CameraError("DLT needs at least 6 matched points")
    T3, T2 = _normalizer(X), _normalizer(uv)
    Xn = np.hstack([X, np.ones((len(X), 1))]) @ T3.T
    un = np.hstack([uv, np.ones((len(uv), 1))]) @ T2.T
    rows = []
    for Xh, (u, v, w) in zip(Xn, un):
        rows.append(np.concatenate([np.zeros(4), -w * Xh, v * Xh]))
        rows.append(np.concatenate([w * Xh, np.zeros(4), -u * Xh]))
    _, _, vt = np.linalg.svd(np.asarray(rows))
    return np.linalg.inv(T2) @ vt[-1].reshape(3, 4) @ T3


def decompose_projection(P: np.ndarray):
    """Split P into (K, R, eye) with K upper triangular, positive diagonal, K[2, 2] = 1."""
    P = np.asarray(P, dtype=float)
    M = P[:, :3]
    if np.linalg.det(M) < 0:
        P, M = -P, -M
    K, R = rq(M)
    D = np.diag(np.sign(np.diag(K)))
    K, R = K @ D, D @ R
    return K / K[2, 2], R, -np.linalg.solve(M, P[:, 3])


def dlt_camera(X: np.ndarray, uv: np.ndarray, image_size: Sequence[int],
               pixel_aspect: float | None = 1.0) -> tuple[Camera | None, str]:
    """A camera from the DLT, or (None, reason) when the DLT is implausible (coplanar points make it so).
    pixel_aspect None keeps the DLT's own fy / fx; a number holds it."""
    try:
        K, R, eye = decompose_projection(dlt_projection(X, uv))
    except (np.linalg.LinAlgError, CameraError) as e:
        return None, f"DLT failed: {e}"
    w, h = image_size
    fx, fy, skew = K[0, 0], K[1, 1], K[0, 1]
    if not (np.isfinite(fx) and np.isfinite(fy)) or fx <= 0 or fy <= 0:
        return None, "DLT gave a non-positive focal length"
    fbar = 0.5 * (fx + fy)
    if not (0.7 < fx / fy < 1.4) or abs(skew) > 0.1 * fbar:
        return None, f"DLT intrinsics implausible (aspect {fx / fy:.3f}, skew {skew / fbar:.3f})"
    f0, a0 = (fx, fy / fx) if pixel_aspect is None else (0.5 * (fx + fy / pixel_aspect), float(pixel_aspect))
    if not (0.2 * w < f0 < 10 * w):
        return None, f"DLT focal length implausible ({f0:.0f} px)"
    cx, cy = K[0, 2], K[1, 2]
    if not (-0.5 * w < cx < 1.5 * w and -0.5 * h < cy < 1.5 * h):
        return None, "DLT principal point far outside the image"
    try:
        cam = Camera(tuple(image_size), f0, R, eye, (cx - w / 2.0, cy - h / 2.0), a0)
    except CameraError as e:
        return None, f"DLT camera invalid: {e}"
    _, z = cam.project(np.asarray(X, float), return_depth=True)
    if np.mean(z > 0) < 0.99:
        return None, "DLT camera puts points behind it"
    return cam, "ok"


def pnp_camera(X: np.ndarray, uv: np.ndarray, image_size: Sequence[int], f: float,
               principal_offset: Sequence[float] = (0.0, 0.0), pixel_aspect: float = 1.0) -> Camera | None:
    """The pose for known intrinsics by OpenCV's SQPnP, which handles coplanar points."""
    X = np.asarray(X, dtype=float).reshape(-1, 1, 3)
    uv = np.asarray(uv, dtype=float).reshape(-1, 1, 2)
    w, h = image_size
    K = np.array([[f, 0, w / 2.0 + principal_offset[0]], [0, pixel_aspect * f, h / 2.0 + principal_offset[1]],
                  [0, 0, 1]])
    # overlay coordinates are OpenCV's shifted by half a pixel on both the points and the principal point, so they
    # pass through as they are
    try:
        ok, rvec, tvec = cv2.solvePnP(X, uv, K, None, flags=getattr(cv2, "SOLVEPNP_SQPNP", cv2.SOLVEPNP_EPNP))
    except cv2.error:
        return None
    if not ok:
        return None
    R, _ = cv2.Rodrigues(rvec)
    try:
        cam = Camera(tuple(image_size), f, R, -R.T @ tvec.reshape(3), tuple(principal_offset), pixel_aspect)
    except CameraError:
        return None
    _, z = cam.project(X.reshape(-1, 3), return_depth=True)
    return cam if np.mean(z > 0) >= 0.5 else None


def camera_start_doc(camera: Camera) -> dict:
    """A camera as a start camera: the camera JSON's pose and intrinsics, nothing else."""
    doc = camera.to_json()
    return {k: doc[k] for k in START_CAMERA_KEYS if k in doc}


def parse_start_camera(value: Any, image_size: Sequence[int]) -> Camera:
    """The Camera a start camera names: a camera JSON object or a Camera, of the photo's size."""
    if isinstance(value, Camera):
        cam = value
    elif isinstance(value, Mapping):
        cam = Camera.from_json({k: v for k, v in value.items() if k in START_CAMERA_KEYS})
    else:
        raise CameraError(f"a start camera must be a camera JSON object or a Camera, got {type(value).__name__}")
    size = (int(image_size[0]), int(image_size[1]))
    if cam.image_size != size:
        raise CameraError(f"the start camera is {cam.image_size[0]}x{cam.image_size[1]}, but the photo is "
                          f"{size[0]}x{size[1]}")
    return cam


# ------------------------------------------------------------ parameterisation


class _Params:
    """Maps a parameter vector to a Camera.

    rot: 'rotvec' (camera-frame perturbation of base.R, radians), 'ypr', 'yr' (pitch 0) or 'y' (pitch and roll 0),
    angles in degrees. focal: 'log', 'linear' or None (held). principal: 'xy', 'y' (dx held) or None (held).
    aspect: 'log', 'linear' or None (held). distortion: 'k1' (k2 held), 'k1k2' or None (held).
    """

    def __init__(self, base: Camera, rot: str, focal: str | None, principal: str | None, aspect: str | None = None,
                 distortion: str | None = None):
        self.base, self.rot, self.focal, self.principal, self.aspect, self.distortion = \
            base, rot, focal, principal, aspect, distortion
        names = {"rotvec": ["rot_tilt", "rot_pan", "rot_roll"], "ypr": ["yaw", "pitch", "roll"],
                 "yr": ["yaw", "roll"], "y": ["yaw"]}[rot] + ["eye_x", "eye_y", "eye_z"]
        names += {"log": ["log_f"], "linear": ["f"], None: []}[focal]
        names += {"xy": ["dx", "dy"], "y": ["dy"], None: []}[principal]
        names += {"log": ["log_aspect"], "linear": ["pixel_aspect"], None: []}[aspect]
        names += {"k1": ["k1"], "k1k2": ["k1", "k2"], None: []}[distortion]
        self.names = names

    def pack(self, cam: Camera) -> np.ndarray:
        if self.rot == "rotvec":
            x = list(Rotation.from_matrix(cam.R @ self.base.R.T).as_rotvec())
        else:
            yaw, pitch, roll = cam.euler_deg()
            x = {"ypr": [yaw, pitch, roll], "yr": [yaw, roll], "y": [yaw]}[self.rot]
        x += list(cam.eye)
        x += {"log": [math.log(cam.f)], "linear": [cam.f], None: []}[self.focal]
        x += {"xy": list(cam.principal_offset), "y": [cam.principal_offset[1]], None: []}[self.principal]
        x += {"log": [math.log(cam.pixel_aspect)], "linear": [cam.pixel_aspect], None: []}[self.aspect]
        x += {"k1": [cam.k1], "k1k2": [cam.k1, cam.k2], None: []}[self.distortion]
        return np.asarray(x, dtype=float)

    def unpack(self, x: np.ndarray) -> Camera:
        if self.rot == "rotvec":
            R, i = Rotation.from_rotvec(x[0:3]).as_matrix() @ self.base.R, 3
        elif self.rot == "ypr":
            R, i = rotation_from_euler(x[0], x[1], x[2]), 3
        elif self.rot == "yr":
            R, i = rotation_from_euler(x[0], 0.0, x[1]), 2
        else:
            R, i = rotation_from_euler(x[0], 0.0, 0.0), 1
        eye = x[i:i + 3]
        i += 3
        f = self.base.f
        if self.focal is not None:
            f = math.exp(min(x[i], 30.0)) if self.focal == "log" else x[i]
            i += 1
        dx, dy = self.base.principal_offset
        if self.principal == "xy":
            dx, dy = x[i], x[i + 1]
            i += 2
        elif self.principal == "y":
            dy = x[i]
            i += 1
        a = self.base.pixel_aspect
        if self.aspect is not None:
            a = math.exp(min(max(x[i], -5.0), 5.0)) if self.aspect == "log" else x[i]
            i += 1
        k1, k2 = self.base.distortion
        if self.distortion == "k1":
            k1 = x[i]
        elif self.distortion == "k1k2":
            k1, k2 = x[i], x[i + 1]
        return Camera(self.base.image_size, max(f, 1e-6), R, eye, (dx, dy), max(a, 1e-6), (k1, k2))


def _constrain(cam: Camera, attitude: str, focal: str, f_fixed: float | None, principal: str,
               principal_offset: Sequence[float], aspect: str, pixel_aspect: float, distortion: str,
               k: Sequence[float]) -> Camera:
    """Force a starting camera onto the solve's constraints. k is the lens the start begins with, or is held at."""
    yaw, pitch, roll = cam.euler_deg()
    R = cam.R
    if attitude == "zero_pitch":
        R = rotation_from_euler(yaw, 0.0, roll)
    elif attitude == "level":
        R = rotation_from_euler(yaw, 0.0, 0.0)
    f, a = cam.f, cam.pixel_aspect
    if aspect == "fixed" and a != pixel_aspect:
        # the horizontal focal length between the start's fx and fy / pixel_aspect
        f, a = 0.5 * (cam.f + cam.fy / pixel_aspect), float(pixel_aspect)
    if focal == "fixed":
        f = f_fixed
    dx, dy = cam.principal_offset
    if principal == "fixed":
        dx, dy = principal_offset
    elif principal == "dy":
        dx = principal_offset[0]
    kd = (0.0, 0.0) if distortion == "none" else (float(k[0]), float(k[1]))
    return Camera(cam.image_size, f, R, cam.eye, (dx, dy), a, kd)


# ------------------------------------------------------------------- problem


def segment_residuals(cam: Camera, direction: np.ndarray, segs: np.ndarray) -> np.ndarray:
    """Pixel distance of each segment's first endpoint from the line joining its midpoint to the vanishing point of
    direction: zero when the segment points at the vanishing point, about half its length times the angular error
    otherwise. Works for vanishing points at infinity; under distortion the endpoints are undistorted first."""
    segs = cam.undistort_segments(segs)
    vp = cam.vanishing_point(direction)
    mid = np.column_stack([(segs[:, 0] + segs[:, 2]) / 2.0, (segs[:, 1] + segs[:, 3]) / 2.0, np.ones(len(segs))])
    lines = np.cross(mid, vp[None, :])
    lines = lines / np.maximum(np.hypot(lines[:, 0], lines[:, 1]), 1e-12)[:, None]
    return np.sum(lines * np.column_stack([segs[:, 0], segs[:, 1], np.ones(len(segs))]), axis=1)


class _Problem:
    """Normalised residuals of picks, segments and priors. prior: None or (dx0, dy0, sigma) on the principal
    point; lens: None or (mode, (k1 mean, sigma), (k2 mean, sigma)) on the coefficients a solve estimates."""

    def __init__(self, corrs: Sequence[Correspondence], segments: Mapping[str, np.ndarray] | None = None,
                 segment_sigma_px: float = 1.0, prior: tuple[float, float, float] | None = None,
                 principal: str = "fixed", lens: tuple | None = None):
        self.corrs = list(corrs)
        self.pi = [i for i, c in enumerate(self.corrs) if c.is_point]
        self.li = [i for i, c in enumerate(self.corrs) if not c.is_point]
        self.X = np.array([self.corrs[i].model for i in self.pi], dtype=float).reshape(-1, 3)
        self.uvp = np.array([self.corrs[i].px for i in self.pi], dtype=float).reshape(-1, 2)
        self.sp = np.array([self.corrs[i].sigma_px for i in self.pi], dtype=float)
        self.LX = np.array([[np.nan if v is None else v for v in self.corrs[i].model] for i in self.li],
                           dtype=float).reshape(-1, 3)
        self.Laxis = np.array([self.corrs[i].free_axis for i in self.li], dtype=int)
        self.Lskew = np.array([self.corrs[i].direction is not None for i in self.li], dtype=bool)
        self.Ldir = np.array([self.corrs[i].direction or (0.0, 0.0, 0.0) for i in self.li], dtype=float).reshape(-1, 3)
        self.uvl = np.array([self.corrs[i].px for i in self.li], dtype=float).reshape(-1, 2)
        self.sl = np.array([self.corrs[i].sigma_px for i in self.li], dtype=float)
        self.segments = [(axis, AXIS_VECTORS[axis], np.asarray(s, dtype=float).reshape(-1, 4))
                         for axis, s in (segments or {}).items() if len(np.asarray(s).reshape(-1, 4))]
        self.segment_sigma = float(segment_sigma_px)
        self.prior, self.principal, self.lens = prior, principal, lens

    def _line_distances(self, cam: Camera) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Signed pixel distances of the line picks from their projected lines, the normals they are measured along,
        and the depth of a point on each line. An axis line's point takes the eye's coordinate on its free axis, so it
        is in front of the camera whenever the line is."""
        n = len(self.li)
        dist, normals = np.zeros(n), np.zeros((n, 2))
        X0 = self.LX.copy()
        X0[np.arange(n), self.Laxis] = cam.eye[self.Laxis]
        for a in range(3):
            sel = (self.Laxis == a) & ~self.Lskew
            if np.any(sel):
                _, dist[sel], normals[sel] = cam.line_foot(self.uvl[sel], X0[sel], AXIS_VECTORS["xyz"[a]])
        for i in np.flatnonzero(self.Lskew):
            d_i = self.Ldir[i]
            X0[i] = self.LX[i] + float((cam.eye - self.LX[i]) @ d_i) * d_i
            _, d, nrm = cam.line_foot(self.uvl[i:i + 1], X0[i:i + 1], d_i)
            dist[i], normals[i] = d[0], nrm[0]
        return dist, normals, cam.to_camera_frame(X0)[:, 2]

    def point_residuals_px(self, cam: Camera) -> tuple[np.ndarray, np.ndarray]:
        """(N, 2) residual vectors (projected - observed; for a line, from the pick to the nearest point of the
        projected line) and (N,) depths, in pick order."""
        n = len(self.corrs)
        vec, depth = np.zeros((n, 2)), np.zeros(n)
        if self.pi:
            Xc = cam.to_camera_frame(self.X)
            z = Xc[:, 2]
            vec[self.pi] = cam._project_normalised(Xc[:, :2] / np.where(z > 1e-3, z, 1e-3)[:, None]) - self.uvp
            depth[self.pi] = z
        if self.li:
            dist, normals, zl = self._line_distances(cam)
            vec[self.li] = -dist[:, None] * normals
            depth[self.li] = zl
        return vec, depth

    def residuals(self, cam: Camera) -> np.ndarray:
        """Normalised rows: 2 per full point, 1 per line, 1 per segment, then the priors."""
        parts = []
        if self.pi:
            vec, _ = self.point_residuals_px(cam)
            parts.append((vec[self.pi] / self.sp[:, None]).ravel())
        if self.li:
            parts.append(self._line_distances(cam)[0] / self.sl)
        for _, d, segs in self.segments:
            parts.append(segment_residuals(cam, d, segs) / self.segment_sigma)
        if self.prior is not None and self.principal != "fixed":
            dx0, dy0, s = self.prior
            dx, dy = cam.principal_offset
            parts.append(np.array([(dx - dx0) / s, (dy - dy0) / s] if self.principal == "free" else [(dy - dy0) / s]))
        if self.lens is not None:
            mode, (m1, s1), (m2, s2) = self.lens
            parts.append(np.array([(cam.k1 - m1) / s1] + ([(cam.k2 - m2) / s2] if mode == "k1k2" else [])))
        r = np.concatenate(parts) if parts else np.zeros(0)
        return np.clip(np.nan_to_num(r, nan=1e6, posinf=1e6, neginf=-1e6), -1e6, 1e6)


def residual_vectors(correspondences: Sequence[Correspondence], camera: Camera) -> tuple[np.ndarray, np.ndarray]:
    """(N, 2) residual vectors and (N,) depths of any picks under any camera (see _Problem.point_residuals_px)."""
    corrs = list(correspondences)
    if not corrs:
        return np.zeros((0, 2)), np.zeros(0)
    return _Problem(corrs).point_residuals_px(camera)


def _numeric_jacobian(fun, x: np.ndarray, rel_step: float = 1e-6) -> np.ndarray:
    f0 = fun(x)
    J = np.empty((f0.size, x.size))
    for i in range(x.size):
        h = rel_step * max(1.0, abs(x[i]))
        xp, xm = x.copy(), x.copy()
        xp[i] += h
        xm[i] -= h
        J[:, i] = (fun(xp) - fun(xm)) / (2.0 * h)
    return J


# -------------------------------------------------------------------- results


@dataclass
class SolveResult:
    camera: Camera
    correspondences: list[Correspondence]
    residual_vectors_px: np.ndarray
    residual_norms_px: np.ndarray
    inliers: np.ndarray
    rms_px: float
    max_px: float
    inlier_rms_px: float
    sigma_ratio: float
    params: dict[str, float]
    std: dict[str, float | None]
    correlations: list[tuple[str, str, float]]
    warnings: list[str]
    candidates: list[dict]
    settings: dict
    # what the picks' layout can and cannot pin: {"coplanar", "collinear", "coverage": [w, h] fractions of the frame}
    geometry: dict = field(default_factory=dict)
    segments_used: dict[str, int] = field(default_factory=dict)
    # the lens posterior when the solve estimated distortion
    lens: dict = field(default_factory=dict)
    # parameters with weight on a direction no row constrains; empty when the solve is determined
    degenerate: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.correspondences)

    @property
    def median_depth_in(self) -> float:
        """The median depth of the picks in front of the camera (a line's at the eye's coordinate on its free axis);
        120 in when none is in front."""
        cam = self.camera
        P = np.array([line_through(c.model, c.direction, cam.eye)[0] if not c.is_point else c.model
                      for c in self.correspondences], dtype=float).reshape(-1, 3)
        depth = cam.to_camera_frame(P)[:, 2]
        good = depth[depth > 0]
        return float(np.median(good)) if len(good) else 120.0

    def correspondence_json(self) -> list[dict]:
        return [correspondence_doc(c, r, bool(ok))
                for c, r, ok in zip(self.correspondences, self.residual_vectors_px, self.inliers)]

    def solve_json(self) -> dict:
        out = {"settings": self.settings, "rms_inlier_px": round(self.inlier_rms_px, 3),
               "sigma_ratio": round(self.sigma_ratio, 3) if math.isfinite(self.sigma_ratio) else None,
               "params": {k: round(v, 6) for k, v in self.params.items()},
               "std": {k: (None if v is None else round(v, 6)) for k, v in self.std.items()},
               "correlations": [[a, b, round(r, 4)] for a, b, r in self.correlations],
               "warnings": list(self.warnings),
               "outliers": [c.label for c, ok in zip(self.correspondences, self.inliers) if not ok],
               "geometry": self.geometry, "segments_used": dict(self.segments_used), "candidates": self.candidates,
               "degenerate": list(self.degenerate)}
        if self.lens:
            out["lens"] = self.lens
        return out

    def summary(self) -> str:
        cam = self.camera
        p, s = self.params, self.std

        def pm(name: str, fmt: str = "{:.2f}") -> str:
            v = p.get(name)
            if v is None or name not in s:
                return "held"
            sd = s.get(name)
            return fmt.format(v) + ("" if sd is None else " +/- " + fmt.format(sd))

        yaw, pitch, roll = cam.euler_deg()
        lines = [
            f"residuals: rms {self.rms_px:.2f} px, max {self.max_px:.2f} px, n {self.n} "
            f"(inlier rms {self.inlier_rms_px:.2f}, sigma ratio {self.sigma_ratio:.2f})",
            f"f {cam.f:.1f} px ({pm('f', '{:.1f}')}), fov_v {cam.fov_v_deg:.2f} deg, fov_h {cam.fov_h_deg:.2f} deg",
            f"pixel aspect fy/fx {cam.pixel_aspect:.4f} ({pm('pixel_aspect', '{:.4f}')})",
            f"principal offset dx {cam.principal_offset[0]:.1f} ({pm('dx', '{:.1f}')}), "
            f"dy {cam.principal_offset[1]:.1f} ({pm('dy', '{:.1f}')}) px",
            f"eye x {pm('eye_x', '{:.1f}')}, y {pm('eye_y', '{:.1f}')}, z {pm('eye_z', '{:.1f}')} in",
            f"yaw {yaw:.2f} ({pm('yaw')}), pitch {pitch:.2f} ({pm('pitch')}), roll {roll:.2f} ({pm('roll')}) deg",
        ]
        if cam.distorted or "k1" in s:
            lines.append(f"lens radial2 k1 {cam.k1:.4f} ({pm('k1', '{:.4f}')}), k2 {cam.k2:.4f} ({pm('k2', '{:.4f}')})")
        lines.append("worst picks:")
        for i in np.argsort(-self.residual_norms_px)[: min(8, self.n)]:
            c = self.correspondences[i]
            kind = "point" if c.is_point else "line " + "xyz"[c.free_axis]
            lines.append(f"  {self.residual_norms_px[i]:7.2f} px  sigma {c.sigma_px:.1f}  {kind:6s}  {c.label}"
                         + ("" if self.inliers[i] else "  OUTLIER"))
        lines += ["WARNING: " + w for w in self.warnings]
        return "\n".join(lines)


def correspondence_doc(c: Correspondence, residual: Sequence[float], inlier: bool | None) -> dict:
    """A pick as the camera JSON records it: label, model, px, its residual and whether the solve kept it (None for a
    held-out check), then its extra facts (the reference, role, note...)."""
    d: dict[str, Any] = {"label": c.label, "model": list(c.model), "px": [round(v, 3) for v in c.px]}
    if c.direction is not None:
        d["direction"] = [round(v, 12) for v in c.direction]
    if c.px_approx is not None:
        d["px_approx"] = [round(v, 3) for v in c.px_approx]
    if c.snap_quality is not None:
        d["snap_quality"] = round(c.snap_quality, 3)
    d.update(sigma_px=c.sigma_px, residual_px=[round(float(v), 3) for v in residual], inlier=inlier)
    if c.note:
        d["note"] = c.note
    for k, v in (c.extra or {}).items():
        d.setdefault(k, v)
    return d


# --------------------------------------------------------------------- checks


def pick_geometry(corrs: Sequence[Correspondence], image_size: Sequence[int]) -> tuple[dict, list[str]]:
    """What the picks' layout can pin, and warnings about it: full points nearly collinear or coplanar (the focal
    length, aspect and principal point then trade against the pose) and picks bunched in a small part of the frame
    (the focal length and principal point are then weakly held)."""
    geo: dict[str, Any] = {"collinear": False, "coplanar": False}
    warns = []
    X = np.array([c.model for c in corrs if c.is_point], dtype=float).reshape(-1, 3)
    if len(X) >= 3:
        sv = np.linalg.svd(X - X.mean(axis=0), compute_uv=False) / math.sqrt(len(X))
        if sv[1] < 0.05 * sv[0]:
            geo["collinear"] = True
            warns.append(f"the full points are nearly collinear (spread {sv[0]:.1f} in along the line, {sv[1]:.1f} in "
                         "across); the pose is not determined")
        elif sv[2] < 0.05 * sv[0]:
            geo["coplanar"] = True
            warns.append(f"the full points are nearly coplanar (rms {sv[2]:.1f} in off their plane over a {sv[0]:.0f} "
                         "in spread); the free intrinsics trade against the pose. Add picks off that plane")
    w, h = image_size
    uv = np.array([c.px for c in corrs], dtype=float).reshape(-1, 2)
    if len(uv) >= 2:
        bw = float((uv[:, 0].max() - uv[:, 0].min()) / w)
        bh = float((uv[:, 1].max() - uv[:, 1].min()) / h)
        geo["coverage"] = [round(bw, 3), round(bh, 3)]
        if bw * bh < 0.2:
            warns.append(f"the picks cover only {100 * bw:.0f}% x {100 * bh:.0f}% of the frame; the focal length and "
                         "principal offset are weakly held. Spread picks to the frame's edges")
    outside = [c.label for c in corrs if not (-0.5 * w <= c.px[0] <= 1.5 * w and -0.5 * h <= c.px[1] <= 1.5 * h)]
    if outside:
        warns.append("pixels far outside the image: " + ", ".join(outside))
    return geo, warns


# ---------------------------------------------------------------------- solve


def solve_camera(
    correspondences: Sequence[Correspondence],
    image_size: Sequence[int],
    *,
    attitude: str = "free",
    principal: str = "free",
    principal_offset: Sequence[float] = (0.0, 0.0),
    principal_prior_px: float | None = None,
    focal: str = "free",
    f: float | None = None,
    aspect: str = "fixed",
    pixel_aspect: float = 1.0,
    aspect_bounds: Sequence[float] = ASPECT_BOUNDS,
    distortion: str = "none",
    k1: float = 0.0,
    k2: float = 0.0,
    k1_prior: Sequence[float] = K1_PRIOR,
    k2_prior: Sequence[float] = K2_PRIOR,
    start_camera: Mapping[str, Any] | Camera | None = None,
    init: Camera | None = None,
    multistart: bool = True,
    segments: Mapping[str, np.ndarray] | None = None,
    segment_sigma_px: float = 1.0,
    loss: str = "soft_l1",
    f_scale: float = 1.5,
    outlier_threshold: float = 3.0,
    fov_h_grid_deg: Sequence[float] = (30, 40, 50, 60, 70, 80, 90, 100, 110),
    fov_h_bounds_deg: Sequence[float] = (10.0, 140.0),
    min_depth_in: float = 6.0,
    max_nfev: int = 600,
) -> SolveResult:
    """Solve a camera from picks.

    attitude    'free' | 'zero_pitch' (verticals held parallel, roll free) | 'level' (pitch and roll 0)
    principal   'free' (dx, dy solved) | 'dy' (dx held at principal_offset[0]) | 'fixed' (held at principal_offset)
    principal_prior_px  optional sigma of a soft prior about principal_offset
    focal       'free' | 'fixed' (f required)
    aspect      'fixed' (held at pixel_aspect) | 'free' (solved from pixel_aspect, bounded to aspect_bounds; a
                solution at a bound is refused)
    distortion  'none' | 'fixed' (held at k1, k2) | 'k1' (k1 solved, k2 held) | 'k1k2'; each solved coefficient
                takes a prior row (k1_prior, k2_prior as (mean, sigma)). A lens that folds over is refused.
    start_camera  an optional starting camera (camera JSON or Camera), recorded in the settings
    init        an optional starting Camera that is not recorded (a provisional solve's answer, a re-solve's old camera)
    multistart  False starts only from init and start_camera, skipping the DLT and the PnP grid: a re-solve near a
                known answer, many times faster
    segments    optional {'x' | 'y' | 'z': (N, 4) segments} that must point at that axis' vanishing point
    loss, f_scale  scipy least_squares' robust loss; residuals are over each pick's sigma, so f_scale is in sigmas
    fov_h_bounds_deg  the free focal length's bounds as a horizontal field of view; a solution at a bound is refused
    min_depth_in  a solution with the camera closer than this to a full point is refused

    Raises CameraError for fewer than six picks or four full points, repeated labels, fewer rows than parameters,
    and solutions that put a full point behind or at the camera or run a free parameter to its bound. Everything
    weaker is reported in SolveResult.warnings, and a direction no row constrains in SolveResult.degenerate.
    """
    corrs = list(correspondences)
    for name, value, allowed in (("attitude", attitude, ATTITUDES), ("principal", principal, PRINCIPALS),
                                 ("focal", focal, FOCALS), ("aspect", aspect, ASPECTS),
                                 ("distortion", distortion, DISTORTIONS)):
        if value not in allowed:
            raise CameraError(f"{name} must be one of {allowed}, got {value!r}")
    if focal == "fixed" and (f is None or not f > 0):
        raise CameraError("focal 'fixed' needs a positive f")
    if isinstance(pixel_aspect, bool) or not isinstance(pixel_aspect, (int, float)) or \
            not (math.isfinite(pixel_aspect) and pixel_aspect > 0):
        raise CameraError(f"pixel_aspect must be a positive number, got {pixel_aspect!r}")
    pixel_aspect = float(pixel_aspect)
    a_lo, a_hi = (float(v) for v in aspect_bounds)
    if not (0.0 < a_lo < a_hi):
        raise CameraError(f"aspect_bounds must satisfy 0 < low < high, got {aspect_bounds}")
    if aspect == "free" and not (a_lo < pixel_aspect < a_hi):
        raise CameraError(f"the starting pixel_aspect {pixel_aspect:g} lies outside aspect_bounds {a_lo:g}-{a_hi:g}")
    for name, v in (("k1", k1), ("k2", k2)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise CameraError(f"{name} must be a finite number, got {v!r}")
    k1, k2 = float(k1), float(k2)
    if distortion == "none" and (k1, k2) != (0.0, 0.0):
        raise CameraError("distortion 'none' holds k1 and k2 at 0; use 'fixed' to hold another lens")
    priors = {}
    for name, pr in (("k1", k1_prior), ("k2", k2_prior)):
        pr = tuple(pr)
        if len(pr) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                                   for v in pr) or not pr[1] > 0:
            raise CameraError(f"{name}_prior must be (mean, sigma) with sigma > 0, got {pr!r}")
        priors[name] = (float(pr[0]), float(pr[1]))
    k1_prior, k2_prior = priors["k1"], priors["k2"]
    di_opt = distortion if distortion in ("k1", "k1k2") else None
    labels = [c.label for c in corrs]
    dup = sorted({x for x in labels if labels.count(x) > 1})
    if dup:
        raise CameraError("duplicate pick labels: " + ", ".join(dup))
    full = [c for c in corrs if c.is_point]
    if len(corrs) < MIN_CORRESPONDENCES:
        raise CameraError(f"need at least {MIN_CORRESPONDENCES} picks, got {len(corrs)}")
    if len(full) < MIN_FULL_POINTS:
        raise CameraError(f"need at least {MIN_FULL_POINTS} picks with all three model coordinates known to start "
                          f"the pose; got {len(full)} plus {len(corrs) - len(full)} line picks")
    lo_deg, hi_deg = (float(v) for v in fov_h_bounds_deg)
    if not (0.0 < lo_deg < hi_deg < 180.0):
        raise CameraError(f"fov_h_bounds_deg must satisfy 0 < low < high < 180, got {fov_h_bounds_deg}")
    image_size = (int(image_size[0]), int(image_size[1]))
    w, h = image_size
    start = None if start_camera is None else parse_start_camera(start_camera, image_size)
    f_lo = (w / 2.0) / math.tan(math.radians(hi_deg) / 2.0)
    f_hi = (w / 2.0) / math.tan(math.radians(lo_deg) / 2.0)
    principal_offset = (float(principal_offset[0]), float(principal_offset[1]))
    geometry, warns = pick_geometry(corrs, image_size)
    prior = None
    if principal_prior_px is not None:
        if not principal_prior_px > 0:
            raise CameraError("principal_prior_px must be positive")
        prior = (principal_offset[0], principal_offset[1], float(principal_prior_px))
    lens = None if di_opt is None else (di_opt, k1_prior, k2_prior)
    problem = _Problem(corrs, segments, segment_sigma_px, prior, principal, lens)
    X = np.array([c.model for c in full], dtype=float)
    uv = np.array([c.px for c in full], dtype=float)

    # ------------------------------------------------------------ candidates
    n_params = {"free": 3, "zero_pitch": 2, "level": 1}[attitude] + 3 + (focal == "free") + \
        {"free": 2, "dy": 1, "fixed": 0}[principal] + (aspect == "free") + {"k1": 1, "k1k2": 2}.get(distortion, 0)
    n_rows = 2 * len(full) + (len(corrs) - len(full)) + sum(len(s) for _, _, s in problem.segments)
    if n_rows < n_params:
        raise CameraError(f"{n_rows} constraint rows cannot determine {n_params} camera parameters")
    cands: list[tuple[str, Camera]] = []
    if init is not None:
        cands.append(("init", init))
    if start is not None:
        cands.append(("start_camera", start))
    dlt, why = None, "skipped: a re-solve starts from its init camera"
    if multistart or not cands:
        dlt, why = (dlt_camera(X, uv, image_size, None if aspect == "free" else pixel_aspect) if len(full) >= 6
                    else (None, "skipped: fewer than 6 full points"))
        if dlt is not None:
            cands.append(("dlt", dlt))
        focal_grid = [f] if focal == "fixed" else [(w / 2.0) / math.tan(math.radians(a) / 2.0) for a in fov_h_grid_deg]
        offsets = [principal_offset]
        if principal != "fixed" and principal_offset != (0.0, 0.0):
            offsets.append((0.0, 0.0) if principal == "free" else (principal_offset[0], 0.0))
        aspects = [pixel_aspect] + ([1.0] if aspect == "free" and pixel_aspect != 1.0 else [])
        for f0 in focal_grid:
            for off in offsets:
                for a0 in aspects:
                    cam = pnp_camera(X, uv, image_size, f0, off, a0)
                    if cam is not None:
                        cands.append((f"pnp f={f0:.0f} offset=({off[0]:.0f},{off[1]:.0f})"
                                      + (f" aspect={a0:.3f}" if aspect == "free" else ""), cam))
    if not cands:
        raise CameraError(f"no starting camera could be formed (DLT: {why}; PnP failed for every focal length)")

    rot_opt = {"free": "rotvec", "zero_pitch": "yr", "level": "y"}[attitude]
    pr_opt = {"free": "xy", "dy": "y", "fixed": None}[principal]
    fo_opt = "log" if focal == "free" else None
    as_opt = "log" if aspect == "free" else None
    n_res = problem.residuals(cands[0][1]).size

    def refine(base: Camera) -> tuple[Camera, float]:
        params = _Params(base, rot_opt, fo_opt, pr_opt, as_opt, di_opt)
        x0 = params.pack(base)
        lo, hi = np.full(x0.size, -np.inf), np.full(x0.size, np.inf)
        bounds = {"log_f": (math.log(f_lo), math.log(f_hi)), "log_aspect": (math.log(a_lo), math.log(a_hi)),
                  "k1": K_BOUNDS, "k2": K_BOUNDS}
        for name, (b0, b1) in bounds.items():
            if name in params.names:
                k = params.names.index(name)
                lo[k], hi[k] = b0, b1
                x0[k] = min(max(x0[k], b0 + 1e-6), b1 - 1e-6)

        def fun(x):
            try:
                return problem.residuals(params.unpack(x))
            except CameraError:
                return np.full(n_res, 1e6)

        sol = least_squares(fun, x0, bounds=(lo, hi), loss=loss, f_scale=f_scale, x_scale="jac", method="trf",
                            max_nfev=max_nfev)
        return params.unpack(sol.x), float(sol.cost)

    tried, best = [], None
    for name, cam0 in cands:
        # a free coefficient of an init camera starts from that camera's own lens
        k_start = (k1, k2)
        if name == "init" and di_opt == "k1":
            k_start = (cam0.k1, k2)
        elif name == "init" and di_opt == "k1k2":
            k_start = cam0.distortion
        try:
            cam, cost = refine(_constrain(cam0, attitude, focal, f, principal, principal_offset, aspect,
                                          pixel_aspect, distortion, k_start))
            if attitude == "free":
                cam, cost = refine(cam)          # re-centre the rotation perturbation and polish
        except (ValueError, np.linalg.LinAlgError, CameraError) as e:
            tried.append({"start": name, "error": str(e)})
            continue
        tried.append({"start": name, "cost": round(cost, 4), "f": round(cam.f, 1)})
        if best is None or cost < best[0]:
            best = (cost, cam, name)
    if best is None:
        raise CameraError("every start failed to converge: " + json.dumps(tried))
    _, cam, start_name = best
    tried.sort(key=lambda d: d.get("cost", float("inf")))

    # ------------------------------------------------ refuse degenerate answers
    vec, depth = problem.point_residuals_px(cam)
    context = ("; layout: " + " | ".join(warns)) if warns else ""
    # a robust loss cannot tell a lone leverage point from a bad pick: name the ones it gave up on
    ratio = np.linalg.norm(vec, axis=1) / np.array([c.sigma_px for c in corrs])
    dropped = [f"{c.label} ({r:.0f} sigma)" for r, c in sorted(zip(ratio, corrs), key=lambda t: -t[0])
               if r > outlier_threshold]
    if dropped:
        context += "; at that solution the robust loss gives up on: " + ", ".join(dropped[:6])
    if focal == "free" and (cam.f <= f_lo * 1.002 or cam.f >= f_hi * 0.998):
        raise CameraError(f"the focal length ran to its bound ({cam.f:.0f} px, fov_h {cam.fov_h_deg:.0f} deg, bounds "
                          f"{lo_deg:g}-{hi_deg:g} deg): the picks do not determine it" + context)
    if aspect == "free" and (cam.pixel_aspect <= a_lo * 1.001 or cam.pixel_aspect >= a_hi * 0.999):
        raise CameraError(f"the pixel aspect ran to its bound ({cam.pixel_aspect:.3f}, bounds {a_lo:g}-{a_hi:g}): "
                          "the picks do not determine it" + context)
    for name in ("k1", "k2")[: {"k1": 1, "k1k2": 2}.get(distortion, 0)]:
        if abs(getattr(cam, name)) >= 0.999 * K_BOUNDS[1]:
            raise CameraError(f"the lens coefficient {name} ran to its bound ({getattr(cam, name):.3f}): the picks do "
                              "not determine it" + context)
    lens_why = cam.lens_problem()
    if lens_why is not None:
        raise CameraError("the best solution's lens cannot describe the photo: " + lens_why + context)
    behind = [c.label for c, z in zip(corrs, depth) if c.is_point and z <= 0]
    if behind:
        raise CameraError("the best solution puts these model points behind the camera: " + ", ".join(behind) + context)
    at_camera = [c.label for c, z in zip(corrs, depth) if c.is_point and 0 < z < min_depth_in]
    if at_camera:
        raise CameraError(f"the best solution puts the camera within {min_depth_in:g} in of: " + ", ".join(at_camera)
                          + " (degenerate)" + context)

    # ------------------------------------------------------------- residuals
    norms = np.linalg.norm(vec, axis=1)
    inl = norms / np.array([c.sigma_px for c in corrs]) < outlier_threshold
    rms = float(np.sqrt(np.mean(norms ** 2)))
    inlier_rms = float(np.sqrt(np.mean(norms[inl] ** 2))) if np.any(inl) else float("nan")
    if not np.all(inl):
        warns.append(f"outliers (> {outlier_threshold:.1f} sigma): " + ", ".join(
            f"{c.label} ({n:.1f} px)" for c, n, ok in zip(corrs, norms, inl) if not ok))

    # ----------------------------------------------------------- uncertainty
    _, pitch, _ = cam.euler_deg()
    rep_rot = {"free": "ypr" if abs(pitch) < 80 else "rotvec", "zero_pitch": "yr", "level": "y"}[attitude]
    rep = _Params(cam, rep_rot, "linear" if focal == "free" else None, pr_opt, "linear" if aspect == "free" else None,
                  di_opt)
    seg_in = {axis: segs[np.abs(segment_residuals(cam, d, segs) / segment_sigma_px) < outlier_threshold]
              for axis, d, segs in problem.segments}
    rep_problem = _Problem([c for c, ok in zip(corrs, inl) if ok], seg_in, segment_sigma_px, prior, principal, lens)
    x_rep = rep.pack(cam)
    params_out = dict(zip(rep.names, (float(v) for v in x_rep)))
    if focal == "fixed":
        params_out["f"] = float(cam.f)          # held, so it has no std; recorded for a re-solve
    std: dict[str, float | None] = {k: None for k in rep.names}
    correlations: list[tuple[str, str, float]] = []
    degenerate: list[str] = []
    m_rows = rep_problem.residuals(cam).size
    dof = m_rows - len(x_rep)
    sigma_ratio = float("nan")
    if dof <= 0:
        warns.append(f"no redundancy: {m_rows} residuals for {len(x_rep)} parameters, so no uncertainty estimate")
    else:
        J = _numeric_jacobian(lambda x: rep_problem.residuals(rep.unpack(x)), x_rep)
        r0 = rep_problem.residuals(cam)
        s2 = float(r0 @ r0) / dof
        sigma_ratio = math.sqrt(s2)
        col = np.linalg.norm(J, axis=0)
        col[col == 0] = 1.0
        Jn = J / col
        _, sv, vt = np.linalg.svd(Jn, full_matrices=False)
        for k_null in np.flatnonzero(sv < 1e-7 * sv[0]):
            null = vt[k_null]
            names = [rep.names[i] for i in np.argsort(-np.abs(null)) if abs(null[i]) > 0.3] or \
                [rep.names[int(np.argmax(np.abs(null)))]]
            degenerate += [nm for nm in names if nm not in degenerate]
            warns.append("the solve is degenerate: no data constrains the combination of " + ", ".join(names))
        cov = np.linalg.pinv(Jn.T @ Jn) / np.outer(col, col) * s2
        sd = np.sqrt(np.clip(np.diag(cov), 0, None))
        std = {k: float(v) for k, v in zip(rep.names, sd)}
        with np.errstate(divide="ignore", invalid="ignore"):
            corr = cov / np.outer(sd, sd)
        for i in range(len(sd)):
            for j in range(i + 1, len(sd)):
                if np.isfinite(corr[i, j]) and abs(corr[i, j]) > 0.9:
                    correlations.append((rep.names[i], rep.names[j], float(corr[i, j])))
        eidx = [rep.names.index(k) for k in ("eye_x", "eye_y", "eye_z")]
        ecov = cov[np.ix_(eidx, eidx)]
        std["eye_along_view"] = float(math.sqrt(max(cam.forward @ ecov @ cam.forward, 0.0)))
        std["eye_largest_axis"] = float(math.sqrt(max(float(np.linalg.eigvalsh(ecov).max()), 0.0)))
        loose = []
        if focal == "free" and std["f"] > 0.05 * cam.f:
            loose.append(f"focal length +/- {100 * std['f'] / cam.f:.0f}%")
        if aspect == "free" and std["pixel_aspect"] > 0.01:
            loose.append(f"pixel aspect +/- {std['pixel_aspect']:.3f}")
        if std["eye_along_view"] > 12.0:
            loose.append(f"eye distance along the view +/- {std['eye_along_view']:.0f} in")
        loose += [f"{k} +/- {std[k]:.0f} px" for k in ("dx", "dy") if std.get(k) is not None and std[k] > 25.0]
        loose += [f"{k} +/- {std[k]:.1f} deg" for k in ("yaw", "pitch", "roll") if std.get(k) is not None and
                  std[k] > 1.0]
        loose += [f"{k} +/- {std[k]:.3f} against its prior sigma {s:g} (the picks barely determine it)"
                  for k, (_, s) in (("k1", k1_prior), ("k2", k2_prior)) if std.get(k) is not None and std[k] > 0.5 * s]
        if loose:
            warns.append("poorly constrained: " + "; ".join(loose))
        strong = [f"{a}~{b} {r:+.3f}" for a, b, r in correlations if abs(r) > 0.98]
        if strong:
            warns.append("strongly correlated parameters: " + ", ".join(strong))
        if sigma_ratio > 2.0:
            warns.append(f"inlier residuals are {sigma_ratio:.1f}x the assumed pixel noise; the model, the picks or "
                         "the camera model are off")
    zc = depth[[i for i, c in enumerate(corrs) if c.is_point]]
    if len(zc) and (zc.max() - zc.min()) < 0.15 * np.median(zc):
        warns.append("the points span little depth relative to their distance; the focal length trades against the "
                     "distance (weak perspective)")

    lens_doc: dict = {}
    if di_opt is not None:
        lens_doc = {"model": DISTORTION_MODEL}
        for k, v, (m, s) in (("k1", cam.k1, k1_prior), ("k2", cam.k2, k2_prior)):
            if k in rep.names:
                sd_k = std.get(k)
                lens_doc[k] = {"mean": round(v, 6), "sigma": None if sd_k is None else round(sd_k, 6),
                               "prior_mean": m, "prior_sigma": s}
            else:
                lens_doc[k] = {"held": round(v, 6)}
    settings = {"attitude": attitude, "principal": principal, "principal_offset": list(principal_offset),
                "principal_prior_px": principal_prior_px, "focal": focal, "f": float(f) if focal == "fixed" else None,
                "aspect": aspect, "pixel_aspect": pixel_aspect, "aspect_bounds": [a_lo, a_hi],
                "distortion": distortion, "k1": k1, "k2": k2, "k1_prior": list(k1_prior), "k2_prior": list(k2_prior),
                "loss": loss, "f_scale": f_scale, "outlier_threshold": outlier_threshold,
                "fov_h_bounds_deg": [lo_deg, hi_deg], "start": start_name, "dlt": "ok" if dlt is not None else why}
    return SolveResult(
        camera=cam, correspondences=corrs, residual_vectors_px=vec, residual_norms_px=norms, inliers=inl,
        rms_px=rms, max_px=float(norms.max()), inlier_rms_px=inlier_rms, sigma_ratio=sigma_ratio,
        params=params_out, std=std, correlations=correlations, warnings=warns, candidates=tried[:12],
        settings=settings, geometry=geometry, segments_used={a: int(len(s)) for a, _, s in problem.segments},
        lens=lens_doc, degenerate=degenerate)
