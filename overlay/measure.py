"""Pixels in solved photos turned into model inches.

    uv run python -m overlay.measure bp STEM PICK [PICK ...] --on TARGET [--offset DX,DY,DZ]
    uv run python -m overlay.measure tri STEM U,V STEM U,V [STEM U,V ...]
    uv run python -m overlay.measure proj STEM FEATURE [FEATURE ...]
    uv run python -m overlay.measure cam STEM [STEM ...]

Every command takes --house PATH and --json.

    STEM     a photo with a solved camera (overlay/cameras/<stem>.json), or a camera JSON path. Its pixel aspect,
             principal offset and lens apply: Camera.ray undistorts a pick before it turns it into a ray.
    PICK     U,V            a point, in overlay pixel coordinates (pixel corners on integers)
             U0,V0,U1,V1    a line pick through two pixels: both ends read on the target
    TARGET   x=V | y=V | z=V  an axis plane
             x=V,y=W          an axis line (any two coordinates; the third free)
             X,Y,Z/X,Y,Z      the line through two model points
             a JSON feature reference (overlay/features.py): one pinned coordinate is that plane (a wall face, a box
             side); a height alone is its surface (a sloped ceiling, a roof plane's top); two pinned coordinates are
             a line (an inside corner)
    FEATURE  X,Y,Z, or a JSON feature reference naming a point or a line

- bp (back-project): a point pick reads where its ray meets the target, and the range along the ray; on a line, the
  line's point nearest the ray and how far the ray passes from it. A line pick reads both ends: the axis it runs
  along, its length and slopes. Several point picks also print the step from each to the next (a width, a rise).
- tri (triangulate): point picks of one feature in two or more photos meet at the point nearest all their rays; each
  ray's miss is printed in inches and pixels, with the widest angle between the rays (a narrow one says little).
- proj: where a model point or line lands in the photo.
- cam: what a camera rests on: the house it was solved on, its rms and its held-out checks.

A reading is only as good as its plane: a pixel on a window head read on the wall's face plane is right only if the
head lies in that plane. Name the plane the feature really lies on. Every reading prints how far it moves per pixel
along u and v, and that step times the camera's held-out check rms (no checks, no such line: nothing measures that
camera away from its picks).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from . import paths
from .camera import Camera, CameraError
from .features import FeatureError, Height, House, Resolved, Surface, resolve_target

AXES = "xyz"
SHOWN_IN = 0.005          # a coordinate moving less than this per px is left out of the sensitivity line
LINE_REACH_IN = 5000.0    # proj samples a model line this far either way


class MeasureError(ValueError):
    """A camera, pick or target that cannot be read, or a reading with no answer."""


# ---------------------------------------------------------------------- cameras


@dataclass(frozen=True)
class PhotoCamera:
    stem: str
    path: Path
    doc: dict
    camera: Camera

    @classmethod
    def load(cls, arg: str | Path) -> "PhotoCamera":
        p = Path(str(arg))
        if not (p.suffix == ".json" and p.exists()):
            p = paths.CAMERAS / f"{paths.photo_stem(p)}.json"
        try:
            doc = json.loads(p.read_text())
            cam = Camera.from_json(doc)
        except (OSError, json.JSONDecodeError, CameraError) as e:
            raise MeasureError(f"{arg}: cannot read the camera {p}: {e}") from None
        return cls(paths.photo_stem(doc.get("photo") or p), p, doc, cam)

    def checks_rms(self) -> float | None:
        return (self.doc.get("check_residual_px") or {}).get("rms")


def camera_report(pc: PhotoCamera, sha_now: str | None) -> dict:
    """The house a camera was solved on against house.json now, its rms and its held-out checks, worst first."""
    checks = sorted(({"label": c.get("label"), "px": round(float(math.hypot(*c["residual_px"])), 2)}
                     for c in pc.doc.get("correspondences") or [] if c.get("role") == "check"), key=lambda c: -c["px"])
    solved = (pc.doc.get("house") or {}).get("sha256")
    return {"photo": pc.stem, "house": solved, "stale": bool(solved and sha_now and solved != sha_now),
            "rms_px": (pc.doc.get("residual_px") or {}).get("rms"), "checks_rms_px": pc.checks_rms(), "checks": checks,
            "forced": bool(((pc.doc.get("solve") or {}).get("guard") or {}).get("forced"))}


def camera_words(rep: dict) -> str:
    words = [f"{rep['photo']}: solved on house {(rep['house'] or '?')[:12]}"
             + (" (house.json has changed since; a re-stamp may move it)" if rep["stale"] else "")]
    if rep["forced"]:
        words.append("written with --force past the write guard")
    if rep["rms_px"] is not None:
        words.append(f"rms {rep['rms_px']:.2f} px")
    if rep["checks"]:
        worst = ", ".join(f"'{c['label']}' {c['px']:.2f}" for c in rep["checks"][:3])
        words.append(f"checks rms {rep['checks_rms_px']:.2f} px (n {len(rep['checks'])}; worst {worst})")
    else:
        words.append("no held-out checks, so nothing measures this camera away from its picks")
    return "; ".join(words)


# ---------------------------------------------------------------------- targets


def _unit(v: Sequence[float]) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(v))
    if not n > 1e-12:
        raise MeasureError("a zero-length direction")
    return v / n


@dataclass(frozen=True)
class Plane:
    """normal . X = offset; axis is the coordinate an axis plane pins."""

    normal: tuple[float, float, float]
    offset: float
    description: str
    axis: int | None = None

    def meet(self, eye: np.ndarray, ray: np.ndarray) -> float | None:
        n = np.asarray(self.normal, dtype=float)
        denom = float(n @ ray)
        t = None if abs(denom) < 1e-12 else (self.offset - float(n @ eye)) / denom
        return t if t is not None and t > 0.0 else None

    def moved(self, o: np.ndarray) -> "Plane":
        return replace(self, offset=self.offset + float(np.asarray(self.normal) @ o))


@dataclass(frozen=True)
class HeightField:
    """A surface z = a x + b y + c over a plan polygon (features.Height): a sloped ceiling, a roof plane's top."""

    height: Height
    description: str
    shift: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def meet(self, eye: np.ndarray, ray: np.ndarray) -> float | None:
        a, b, c = self.height.coef
        dx, dy, dz = self.shift
        c = c + dz - a * dx - b * dy
        denom = float(ray[2] - (a * ray[0] + b * ray[1]))
        if abs(denom) < 1e-12:
            return None
        t = -float(eye[2] - (a * eye[0] + b * eye[1] + c)) / denom
        if not t > 0.0:
            return None
        x, y = eye[0] + t * ray[0] - dx, eye[1] + t * ray[1] - dy
        return t if self.height.z_at(x, y) is not None else None

    def moved(self, o: np.ndarray) -> "HeightField":
        return replace(self, shift=tuple(float(s + v) for s, v in zip(self.shift, o)))


@dataclass(frozen=True)
class Line:
    """point + s direction, direction a unit vector."""

    point: tuple[float, float, float]
    direction: tuple[float, float, float]
    description: str

    def moved(self, o: np.ndarray) -> "Line":
        return replace(self, point=tuple(float(v) for v in np.asarray(self.point) + o))


Target = Plane | HeightField | Line


def _floats(text: str, n: int | None, what: str) -> list[float]:
    try:
        vals = [float(v) for v in text.split(",")]
    except ValueError:
        raise MeasureError(f"{text!r}: expected {what}") from None
    if (n is not None and len(vals) != n) or not all(math.isfinite(v) for v in vals):
        raise MeasureError(f"{text!r}: expected {what}")
    return vals


def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _axis_vec(k: int) -> tuple[float, float, float]:
    d = [0.0, 0.0, 0.0]
    d[k] = 1.0
    return tuple(d)


def target_from_reference(ref: Any, house: House) -> Target:
    """The plane, height surface or line a feature reference names."""
    try:
        r = resolve_target(house, ref, where="target")
    except FeatureError as e:
        raise MeasureError(str(e)) from None
    if isinstance(r, Surface):
        if r.axis is not None:
            return Plane(_axis_vec(r.axis), float(r.value), f"{AXES[r.axis]}={_fmt(r.value)} ({r.description})", r.axis)
        return HeightField(r.height, f"{r.height.what} of {r.height.element}")
    assert isinstance(r, Resolved)
    if r.direction is not None:
        return Line(tuple(float(v) for v in r.model), r.direction, f"the line along {r.direction} ({r.description})")
    if r.free_axis is None:
        raise MeasureError(f"target {r.description} names the point {[round(v, 2) for v in r.model]}; bp reads on a "
                           "surface or a line (proj projects a point)")
    k = r.free_axis
    words = ",".join(f"{AXES[i]}={_fmt(r.model[i])}" for i in range(3) if i != k)
    return Line(tuple(0.0 if v is None else float(v) for v in r.model), _axis_vec(k), f"{words} ({r.description})")


def parse_target(text: str, house: Callable[[], House]) -> Target:
    """A TARGET argument (module doc); house is called only for a feature reference."""
    s = text.strip()
    if s[:1] in "[{":
        try:
            ref = json.loads(s)
        except json.JSONDecodeError as e:
            raise MeasureError(f"target {s!r}: not a JSON feature reference: {e}") from None
        return target_from_reference(ref, house())
    if "/" in s:
        parts = s.split("/")
        if len(parts) != 2:
            raise MeasureError(f"target {s!r}: a line through two model points is X,Y,Z/X,Y,Z")
        a, b = (np.array(_floats(p, 3, "a model point X,Y,Z"), dtype=float) for p in parts)
        return Line(tuple(a), tuple(_unit(b - a)), f"the line through {a.round(2).tolist()} and {b.round(2).tolist()}")
    if "=" in s:
        pins: dict[int, float] = {}
        for part in s.split(","):
            key, _, value = part.partition("=")
            key = key.strip().lower()
            if key not in AXES or AXES.index(key) in pins:
                raise MeasureError(f"target {s!r}: expected x=V, y=V or z=V, each axis at most once")
            pins[AXES.index(key)] = _floats(value, 1, f"a number after {key}=")[0]
        if len(pins) == 1:
            (k, v), = pins.items()
            return Plane(_axis_vec(k), v, f"{AXES[k]}={_fmt(v)}", k)
        if len(pins) == 2:
            k = next(i for i in range(3) if i not in pins)
            return Line(tuple(pins.get(i, 0.0) for i in range(3)), _axis_vec(k),
                        ",".join(f"{AXES[i]}={_fmt(v)}" for i, v in sorted(pins.items())))
        raise MeasureError(f"target {s!r}: x=, y= and z= together name a point; bp reads on a surface or a line")
    raise MeasureError(f"target {s!r}: expected x=V, x=V,y=W, X,Y,Z/X,Y,Z or a JSON feature reference")


def moved(target: Target, offset: str | None) -> Target:
    if not offset:
        return target
    o = np.array(_floats(offset, 3, "an offset DX,DY,DZ"), dtype=float)
    return replace(target.moved(o), description=f"{target.description} moved by {o.round(3).tolist()}")


# --------------------------------------------------------------------- readings


def read_point(cam: Camera, uv: Sequence[float], target: Target) -> tuple[np.ndarray, dict]:
    """The model point a pixel reads on a target, the range along the ray and, on a line, how far the ray passes from
    it (inches and pixels)."""
    e, r = cam.eye, cam.ray(np.asarray(uv, dtype=float).reshape(1, 2))[0]
    if isinstance(target, Line):
        p, d = np.asarray(target.point, dtype=float), np.asarray(target.direction, dtype=float)
        w0 = p - e
        b, dd, ee = float(d @ r), float(d @ w0), float(r @ w0)
        den = 1.0 - b * b
        if den < 1e-12:
            raise MeasureError(f"the ray through ({uv[0]:.2f}, {uv[1]:.2f}) runs along {target.description}")
        s = (b * ee - dd) / den
        t = ee + b * s
        if not t > 0.0:
            raise MeasureError(f"{target.description} passes nearest the ray through ({uv[0]:.2f}, {uv[1]:.2f}) "
                               "behind the camera")
        X = p + s * d
        return X, {"range_in": t, "miss_in": float(np.linalg.norm(X - (e + t * r))),
                   "miss_px": float(np.linalg.norm(cam.project(X) - np.asarray(uv, dtype=float)))}
    t = target.meet(e, r)
    if t is None:
        raise MeasureError(f"the ray through ({uv[0]:.2f}, {uv[1]:.2f}) meets {target.description} nowhere in front "
                           "of the camera")
    return e + t * r, {"range_in": t}


def _steps(fn: Callable[[np.ndarray], np.ndarray], uv: np.ndarray, directions: Sequence[np.ndarray]) -> list:
    """How far fn's reading moves for 1 px along each direction (central differences), None where it reads nothing."""
    out = []
    for step in directions:
        try:
            out.append(0.5 * (fn(uv + step) - fn(uv - step)))
        except MeasureError:
            out.append(None)
    return out


def _at_checks(steps: Sequence[np.ndarray | None], rms: float | None) -> list[float] | None:
    """Each coordinate's movement at the camera's check rms: the per-px steps in quadrature, times the rms."""
    if rms is None or any(s is None for s in steps):
        return None
    return [round(float(rms * math.sqrt(sum(float(s[k]) ** 2 for s in steps))), 3) for k in range(3)]


def _r(v: Any, nd: int = 3) -> Any:
    if v is None:
        return None
    if isinstance(v, np.ndarray):
        return [round(float(x), nd) for x in v]
    return round(float(v), nd)


UV_STEPS = (np.array([1.0, 0.0]), np.array([0.0, 1.0]))


def bp_point(pc: PhotoCamera, uv: Sequence[float], target: Target) -> dict:
    uv = np.asarray(uv, dtype=float)
    X, info = read_point(pc.camera, uv, target)
    steps = _steps(lambda q: read_point(pc.camera, q, target)[0], uv, UV_STEPS)
    return {"kind": "point", "photo": pc.stem, "px": _r(uv), "target": target.description, "model": _r(X),
            **{k: _r(v) for k, v in info.items()}, "per_px_u": _r(steps[0]), "per_px_v": _r(steps[1]),
            "at_checks_rms_in": _at_checks(steps, pc.checks_rms()),
            "pinned_axis": target.axis if isinstance(target, Plane) else None}


def bp_line(pc: PhotoCamera, ends: Sequence[Sequence[float]], target: Target) -> dict:
    if isinstance(target, Line):
        raise MeasureError("a line pick reads on a surface; on a line give a point pick")
    ends = np.asarray(ends, dtype=float)
    X0, i0 = read_point(pc.camera, ends[0], target)
    X1, i1 = read_point(pc.camera, ends[1], target)
    d = X1 - X0
    along = int(np.argmax(np.abs(d)))
    if not abs(d[along]) > 1e-9:
        raise MeasureError("both ends of the line pick read the same point")
    pinned = target.axis if isinstance(target, Plane) else None
    return {"kind": "line", "photo": pc.stem, "px": [_r(e) for e in ends], "target": target.description,
            "ends": [_r(X0), _r(X1)], "range_in": [_r(i0["range_in"]), _r(i1["range_in"])],
            "runs_along": AXES[along], "length_in": _r(float(np.linalg.norm(d))),
            "slopes": {f"d{AXES[k]}/d{AXES[along]}": _r(float(d[k] / d[along]), 5) for k in range(3)
                       if k != along and k != pinned}}


def triangulate(rays: Sequence[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
    """The point nearest every ray (eye, unit direction), least squares."""
    A, b = np.zeros((3, 3)), np.zeros(3)
    for e, r in rays:
        M = np.eye(3) - np.outer(r, r)
        A += M
        b += M @ e
    lam = np.linalg.eigvalsh(A)
    if lam[0] < 1e-6 * max(lam[-1], 1e-300):
        raise MeasureError("the rays are parallel, so they meet nowhere")
    return np.linalg.solve(A, b)


def tri_points(items: Sequence[tuple[PhotoCamera, np.ndarray]]) -> dict:
    cams = [pc.camera for pc, _ in items]
    uvs = [np.asarray(uv, dtype=float) for _, uv in items]

    def meet(moved_uvs):
        return triangulate([(c.eye, c.ray(q.reshape(1, 2))[0]) for c, q in zip(cams, moved_uvs)])

    X = meet(uvs)
    rays = [c.ray(q.reshape(1, 2))[0] for c, q in zip(cams, uvs)]
    per, total, complete = [], np.zeros(3), True
    for i, ((pc, _), c, q, r) in enumerate(zip(items, cams, uvs, rays)):
        rng = float(r @ (X - c.eye))
        if not rng > 0.0:
            raise MeasureError(f"{pc.stem}: the meeting point lies behind the camera; the picks are not one feature")
        steps = _steps(lambda qq, i=i: meet([qq if j == i else u for j, u in enumerate(uvs)]), q, UV_STEPS)
        ck = _at_checks(steps, pc.checks_rms())
        if ck is None:
            complete = False
        else:
            total += np.square(ck)
        per.append({"photo": pc.stem, "px": _r(q), "range_in": _r(rng),
                    "miss_in": _r(float(np.linalg.norm((np.eye(3) - np.outer(r, r)) @ (X - c.eye)))),
                    "miss_px": _r(float(np.linalg.norm(c.project(X) - q))), "per_px_u": _r(steps[0]),
                    "per_px_v": _r(steps[1])})
    widest = max(math.degrees(math.acos(max(-1.0, min(1.0, float(a @ b))))) for i, a in enumerate(rays)
                 for b in rays[i + 1:])
    return {"kind": "tri_point", "model": _r(X), "widest_angle_deg": _r(widest, 2), "photos": per,
            "at_checks_rms_in": [round(float(v), 3) for v in np.sqrt(total)] if complete else None}


def project_feature(pc: PhotoCamera, text: str, house: Callable[[], House]) -> dict:
    """Where a model point (X,Y,Z or a reference) or line (a reference with a free axis) lands in the photo."""
    cam = pc.camera
    w, h = cam.image_size
    s = text.strip()
    d = None
    if s[:1] in "[{":
        try:
            r = resolve_target(house(), json.loads(s), where="feature")
        except (FeatureError, json.JSONDecodeError) as e:
            raise MeasureError(f"feature {s!r}: {e}") from None
        if isinstance(r, Surface):
            raise MeasureError(f"feature {r.description} names a surface; proj projects a point or a line")
        X, words = np.array([0.0 if v is None else v for v in r.model], dtype=float), r.description
        if r.direction is not None:
            d = np.asarray(r.direction, dtype=float)
        elif r.free_axis is not None:
            d = np.asarray(_axis_vec(r.free_axis))
    else:
        X, words = np.array(_floats(s, 3, "a model point X,Y,Z or a feature reference"), dtype=float), s
    if d is None:
        uv, depth = cam.project(X, return_depth=True)
        return {"kind": "proj_point", "photo": pc.stem, "feature": words, "model": _r(X), "px": _r(uv),
                "depth_in": _r(float(depth)), "in_frame": bool(depth > 0 and 0 <= uv[0] <= w and 0 <= uv[1] <= h)}
    pts = X[None, :] + np.linspace(-LINE_REACH_IN, LINE_REACH_IN, 4001)[:, None] * d[None, :]
    uv, depth = cam.project(pts, return_depth=True)
    ok = np.flatnonzero((depth > 1.0) & (uv[:, 0] >= 0) & (uv[:, 0] <= w) & (uv[:, 1] >= 0) & (uv[:, 1] <= h))
    if not len(ok):
        return {"kind": "proj_line", "photo": pc.stem, "feature": words, "px": [], "in_frame": False}
    return {"kind": "proj_line", "photo": pc.stem, "feature": words, "in_frame": True,
            "px": [_r(uv[ok[0]]), _r(uv[ok[len(ok) // 2]]), _r(uv[ok[-1]])]}


# ---------------------------------------------------------------------- printing


def _coords(v: Sequence[float] | None, skip: int | None = None, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    shown = [k for k in range(3) if k != skip and (not signed or abs(v[k]) >= SHOWN_IN)]
    if not shown:
        return f"under {SHOWN_IN * 2:g} in"
    return " ".join(f"{AXES[k]} {v[k]:+.2f}" if signed else f"{AXES[k]} {v[k]:.2f}" for k in shown)


def words(res: dict) -> list[str]:
    kind = res["kind"]
    if kind == "point":
        line = (f"{res['photo']} ({res['px'][0]:.2f}, {res['px'][1]:.2f}) on {res['target']}: "
                f"{_coords(res['model'])}, range {res['range_in']:.1f} in")
        if "miss_in" in res:
            line += f"; the ray passes {res['miss_in']:.2f} in ({res['miss_px']:.2f} px) from the line"
        sens = f"    per px u ({_coords(res['per_px_u'], res['pinned_axis'], True)}), v " \
               f"({_coords(res['per_px_v'], res['pinned_axis'], True)})"
        if res["at_checks_rms_in"] is not None:
            sens += f"; at the checks' rms: {_coords(res['at_checks_rms_in'], res['pinned_axis'])} in"
        return [line, sens]
    if kind == "line":
        slopes = ", ".join(f"{k} {v:+.4f}" for k, v in res["slopes"].items())
        return [f"{res['photo']} line pick on {res['target']}: runs along {res['runs_along']} {res['length_in']:.2f} in"
                + (f", {slopes}" if slopes else ""),
                f"    end A {_coords(res['ends'][0])}; end B {_coords(res['ends'][1])}"]
    if kind == "tri_point":
        out = [f"meet of {len(res['photos'])} rays: {_coords(res['model'])}, widest angle "
               f"{res['widest_angle_deg']:.1f} deg" + ("" if res["at_checks_rms_in"] is None else
                                                       f"; at the checks' rms: {_coords(res['at_checks_rms_in'])} in")]
        out += [f"    {p['photo']} ({p['px'][0]:.2f}, {p['px'][1]:.2f}): misses by {p['miss_in']:.2f} in "
                f"({p['miss_px']:.2f} px) at range {p['range_in']:.1f} in" for p in res["photos"]]
        return out
    if kind == "proj_point":
        where = "in" if res["in_frame"] else "outside"
        return [f"{res['photo']} {res['feature']} {_coords(res['model'])}: px ({res['px'][0]:.2f}, "
                f"{res['px'][1]:.2f}), depth {res['depth_in']:.1f} in, {where} the frame"]
    if not res["in_frame"]:
        return [f"{res['photo']} {res['feature']}: the line misses the frame"]
    return [f"{res['photo']} {res['feature']}: px " + " .. ".join(f"({u:.2f}, {v:.2f})" for u, v in res["px"])
            + " (first, middle, last in the frame)"]


# -------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m overlay.measure", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--house", type=Path, default=paths.HOUSE)
    common.add_argument("--json", action="store_true", help="print the readings as JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bp", parents=[common], help="back-project picks onto a plane, surface or line")
    p.add_argument("photo")
    p.add_argument("picks", nargs="+")
    p.add_argument("--on", required=True, help="the TARGET; a value starting with - goes after = (--on=-58.7,...)")
    p.add_argument("--offset", help="DX,DY,DZ: move the target (an unmodelled feature beside a modelled face)")
    p = sub.add_parser("tri", parents=[common], help="meet point picks of one feature in several photos")
    p.add_argument("items", nargs="+", help="STEM U,V STEM U,V ...")
    p = sub.add_parser("proj", parents=[common], help="project model points or lines into a photo")
    p.add_argument("photo")
    p.add_argument("features", nargs="+")
    p = sub.add_parser("cam", parents=[common], help="what a camera rests on")
    p.add_argument("photos", nargs="+")
    a = ap.parse_args(argv)
    house_cache: list[House] = []

    def house() -> House:
        if not house_cache:
            try:
                house_cache.append(House.load(a.house))
            except (OSError, FeatureError) as e:
                raise MeasureError(f"cannot load {a.house}: {e}") from None
        return house_cache[0]

    try:
        sha_now = hashlib.sha256(Path(a.house).read_bytes()).hexdigest()
    except OSError:
        sha_now = None
    cameras: dict[str, dict] = {}

    def camera(arg: str) -> PhotoCamera:
        pc = PhotoCamera.load(arg)
        cameras.setdefault(pc.stem, camera_report(pc, sha_now))
        return pc

    results: list[dict] = []
    try:
        if a.cmd == "bp":
            pc = camera(a.photo)
            target = moved(parse_target(a.on, house), a.offset)
            for text in a.picks:
                vals = _floats(text, None, "a pick U,V or a line pick U0,V0,U1,V1")
                if len(vals) == 2:
                    results.append(bp_point(pc, vals, target))
                elif len(vals) == 4:
                    results.append(bp_line(pc, [vals[:2], vals[2:]], target))
                else:
                    raise MeasureError(f"pick {text!r}: expected U,V or U0,V0,U1,V1")
        elif a.cmd == "tri":
            if len(a.items) < 4 or len(a.items) % 2:
                raise MeasureError("tri takes STEM U,V pairs, at least two of them")
            results.append(tri_points([(camera(s), np.array(_floats(t, 2, "a pick U,V")))
                                       for s, t in zip(a.items[0::2], a.items[1::2])]))
        elif a.cmd == "proj":
            pc = camera(a.photo)
            results += [project_feature(pc, text, house) for text in a.features]
        else:
            for photo in a.photos:
                camera(photo)
    except MeasureError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"readings": results, "cameras": list(cameras.values())}, indent=1))
        return 0
    for res in results:
        print("\n".join(words(res)))
    pts = [r for r in results if r["kind"] == "point"]
    for p, q in zip(pts, pts[1:]):
        d = np.subtract(q["model"], p["model"])
        print(f"step to ({q['px'][0]:.2f}, {q['px'][1]:.2f}): {_coords(d, None, True)}, "
              f"{float(np.linalg.norm(d)):.2f} in")
    for rep in cameras.values():
        print(camera_words(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
