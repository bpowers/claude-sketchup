"""A photo's picks file, overlay/picks/<stem>.json: load, validate, resolve, snap.

    {"photo": "living-01.jpg",
     "notes": ["The living room from beside the kitchen opening, toward the front door and the west window"],
     "open_leaves": ["living.wall.front.door"],
     "points": [
      {"label": "front window: casing's inner corner, start jamb at the head", "px": [606.6, 371.7], "snap": true,
       "ref": {"el": "living.wall.front.window", "jamb": "start", "level": "head", "face": "left", "casing": "inner"}},
      {"label": "inside corner, front and west walls", "px": [1118.2, 600.0],
       "ref": [{"el": "living.wall.front", "face": "left"}, {"el": "living.wall.west", "face": "+x"}], "free": "z"},
      {"label": "sofa arm, front corner", "px": [655.0, 590.0], "role": "check",
       "model": [180.0, 96.0, 25.0], "explicit_note": "furniture, not in the model; taped"}
     ]}

- ``label``: unique within the file. ``px``: [u, v] in overlay image coordinates (pixel corners on integers;
  camera.py), read off the photo as it is.
- Exactly one of ``ref`` and ``model``. ``ref`` names model features (grammar in features.py) and is resolved
  against house.json when the file is loaded. ``model`` is explicit [x, y, z] inches, only for things the model
  does not contain, with an ``explicit_note`` saying what and why; one coordinate may be null (a line along that
  axis), or with all three a ``direction`` [dx, dy, dz] makes a line along it.
- ``free``: the axis a reference leaves unpinned, declared, which makes the pick a line pick (the pixel lies on the
  model line along that axis).
- ``offset`` [dx, dy, dz] with ``offset_note`` moves a resolved reference, for an unmodelled feature a known
  distance from a modelled one.
- ``role``: ``solve`` (default) or ``check``. A check is held out of the solve and measured with the solved
  camera, so a disagreement is reported instead of absorbed.
- ``snap`` (default false): refine the pick, to a corner for a point, onto the nearest straight edge for a line.
- ``sigma_px``: the expected error in pixels, combining how well the pick is read and how well the model position
  is known. Default 6 px by eye, 1.5 px after a successful snap; an explicit value always wins.
- ``open_leaves`` (top level): door openings whose leaves stand open in the photo; each must be a door with a
  unit. The render request hides their leaves.

Every problem raises PicksError naming the file, the label and the field or element.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np

from . import paths
from .camera import CameraError, Correspondence, from_cv, to_cv
from .features import FeatureError, House, resolve
from .vanishing import detect_segments, segment_lengths, to_gray

SIGMA_BY_EYE_PX = 6.0
SIGMA_SNAPPED_PX = 1.5
FIELDS = ("label", "px", "ref", "free", "offset", "offset_note", "model", "explicit_note", "direction", "role",
          "snap", "sigma_px", "note")
ROLES = ("solve", "check")


class PicksError(ValueError):
    pass


@dataclass
class PickSet:
    photo: str
    path: Path | None
    points: list[Correspondence]
    notes: list[str] = field(default_factory=list)
    open_leaves: list[str] = field(default_factory=list)
    explicit_sigma: dict[str, bool] = field(default_factory=dict)
    snaps: dict[str, "SnapResult"] = field(default_factory=dict)

    @property
    def solve_points(self) -> list[Correspondence]:
        return [c for c in self.points if c.role == "solve"]

    @property
    def checks(self) -> list[Correspondence]:
        return [c for c in self.points if c.role == "check"]


def picks_path(stem_or_path: str | Path) -> Path:
    p = Path(str(stem_or_path))
    if p.suffix == ".json" and p.exists():
        return p
    return paths.PICKS / f"{paths.photo_stem(p)}.json"


def open_leaves_problems(doc: Any, ids: Sequence[str]) -> list[str]:
    """Why each id cannot be drawn as an open door: it must be an opening of kind door with a unit."""
    found = {op["id"]: op for el in doc.get("elements", []) if isinstance(el, dict) and el.get("type") == "wall"
             for op in el.get("openings") or [] if isinstance(op, dict) and isinstance(op.get("id"), str)}
    problems = []
    for i in ids:
        op = found.get(i)
        if op is None:
            problems.append(f"{i} is not an opening in house.json")
        elif op.get("kind") != "door":
            problems.append(f"{i} is a {op.get('kind')} opening, and only a door's leaves open")
        elif not isinstance(op.get("unit"), dict):
            problems.append(f"{i} is a door with no unit, drawn as a plain hole already")
    return problems


def _note(p: dict, key: str, where: str, label: str) -> str:
    v = p.get(key)
    if not isinstance(v, str) or not v.strip():
        raise PicksError(f"{where}: '{label}' needs a non-empty {key}")
    return v


def parse(doc: Any, house: House, path: Path | None = None) -> PickSet:
    """Validate a picks document and resolve its references against house."""
    where = str(path) if path else "picks"
    if not isinstance(doc, dict):
        raise PicksError(f"{where}: the top level must be an object")
    unknown = set(doc) - {"photo", "notes", "open_leaves", "points"}
    if unknown:
        raise PicksError(f"{where}: unknown top-level keys {sorted(unknown)}")
    photo = doc.get("photo")
    if not isinstance(photo, str) or not photo:
        raise PicksError(f"{where}: 'photo' must be a file name such as 'living-01.jpg'")
    pts = doc.get("points")
    if not isinstance(pts, list) or not pts:
        raise PicksError(f"{where}: 'points' must be a non-empty list")
    leaves = doc.get("open_leaves", [])
    if not (isinstance(leaves, list) and all(isinstance(i, str) and i.strip() for i in leaves)) or \
            len(set(leaves)) != len(leaves):
        raise PicksError(f"{where}: 'open_leaves' must be a list of distinct door opening ids, got {leaves!r}")
    problems = open_leaves_problems(house.doc, leaves)
    if problems:
        raise PicksError(f"{where}: open_leaves: " + "; ".join(problems))
    out, explicit, seen = [], {}, set()
    for i, p in enumerate(pts):
        if not isinstance(p, dict):
            raise PicksError(f"{where}: points[{i}] must be an object")
        label = p.get("label")
        if not isinstance(label, str) or not label.strip():
            raise PicksError(f"{where}: points[{i}] needs a non-empty 'label'")
        if label in seen:
            raise PicksError(f"{where}: duplicate label '{label}'")
        seen.add(label)
        unknown = set(p) - set(FIELDS)
        if unknown:
            raise PicksError(f"{where}: '{label}' has unknown fields {sorted(unknown)}")
        px = p.get("px")
        if not (isinstance(px, list) and len(px) == 2 and
                all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in px)):
            raise PicksError(f"{where}: '{label}' px must be [u, v] numbers, got {px!r}")
        if ("ref" in p) == ("model" in p):
            raise PicksError(f"{where}: '{label}' needs exactly one of ref (a model feature) and model (explicit "
                             "coordinates for something the model does not contain)")
        extra: dict[str, Any] = {}
        if "model" in p:
            model = p["model"]
            if not (isinstance(model, list) and len(model) == 3 and
                    all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)) for v in model)):
                raise PicksError(f"{where}: '{label}' model must be [x, y, z] numbers or null, got {model!r}")
            for key in ("free", "offset", "offset_note"):
                if key in p:
                    raise PicksError(f"{where}: '{label}' {key} belongs with ref, not with explicit model")
            extra["explicit_note"] = _note(p, "explicit_note", where, label)
            if "direction" in p:
                d = p["direction"]
                if not (isinstance(d, list) and len(d) == 3 and
                        all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in d)):
                    raise PicksError(f"{where}: '{label}' direction must be [dx, dy, dz] numbers, got {d!r}")
                extra["direction"] = [float(v) for v in d]
        else:
            for key in ("explicit_note", "direction"):
                if key in p:
                    raise PicksError(f"{where}: '{label}' {key} belongs with explicit model coordinates")
            if ("offset" in p) != ("offset_note" in p):
                raise PicksError(f"{where}: '{label}' offset and offset_note go together")
            try:
                res = resolve(house, p["ref"], free=p.get("free"), offset=p.get("offset"), where=f"{where}: '{label}'")
            except FeatureError as e:
                raise PicksError(str(e)) from None
            model = list(res.model)
            extra.update(ref=p["ref"], feature=res.description, model_src=dict(res.srcs))
            if res.direction is not None:
                extra["direction"] = list(res.direction)
            if "free" in p:
                extra["free"] = p["free"]
            if "offset" in p:
                extra.update(offset=list(p["offset"]), offset_note=_note(p, "offset_note", where, label))
        role = p.get("role", "solve")
        if role not in ROLES:
            raise PicksError(f"{where}: '{label}' role must be one of {', '.join(ROLES)}, got {role!r}")
        if role != "solve":
            extra["role"] = role
        snap = p.get("snap", False)
        if not isinstance(snap, bool):
            raise PicksError(f"{where}: '{label}' snap must be true or false")
        sigma = p.get("sigma_px")
        if sigma is not None and not (isinstance(sigma, (int, float)) and not isinstance(sigma, bool) and sigma > 0):
            raise PicksError(f"{where}: '{label}' sigma_px must be a positive number")
        explicit[label] = sigma is not None
        try:
            out.append(Correspondence(label, tuple(px), tuple(model), sigma_px=float(sigma or SIGMA_BY_EYE_PX),
                                      snap=snap, note=p.get("note"), extra=extra, direction=extra.get("direction")))
        except CameraError as e:
            raise PicksError(f"{where}: {e}") from None
    notes = doc.get("notes", [])
    return PickSet(photo, path, out, [notes] if isinstance(notes, str) else list(notes), list(leaves), explicit)


def load(stem_or_path: str | Path, house: House) -> PickSet:
    path = picks_path(stem_or_path)
    if not path.exists():
        raise FileNotFoundError(f"no picks at {path}")
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise PicksError(f"{path}: invalid JSON: {e}") from e
    return parse(doc, house, path)


def reresolve(pick_doc: dict, house: House) -> tuple[tuple | None, str | None]:
    """A camera JSON pick's reference resolved against a (newer) house: (model, None), (None, error), or (None, None)
    for an explicit pick."""
    if "ref" not in pick_doc:
        return None, None
    try:
        res = resolve(house, pick_doc["ref"], free=pick_doc.get("free"), offset=pick_doc.get("offset"),
                      where=f"'{pick_doc.get('label')}'")
    except FeatureError as e:
        return None, str(e)
    return res.model, None


# ------------------------------------------------------------------- snapping


@dataclass
class SnapResult:
    px: tuple[float, float]
    ok: bool
    quality: float
    method: str
    moved_px: float
    detail: str


def snap_all(picks: PickSet, image: np.ndarray, *, radius: float = 12.0,
             min_quality: float = 0.3) -> tuple[PickSet, list[str]]:
    """Snap every pick marked snap: true. Returns a new set and a warning per failed snap; a failed snap keeps the
    by-eye pick at the by-eye sigma."""
    gray = to_gray(image).astype(np.float32)
    warns, points, snaps = [], [], {}
    for c in picks.points:
        if not c.snap:
            points.append(c)
            continue
        r = (snap_corner if c.is_point else snap_to_edge)(gray, c.px, radius=radius, min_quality=min_quality)
        snaps[c.label] = r
        keep = picks.explicit_sigma.get(c.label, False)
        if r.ok:
            points.append(replace(c, px=r.px, px_approx=c.px, snap_quality=r.quality,
                                  sigma_px=c.sigma_px if keep else SIGMA_SNAPPED_PX))
        else:
            warns.append(f"snap failed for '{c.label}' ({r.detail}); using the by-eye pick")
            points.append(replace(c, snap_quality=0.0))
    return replace(picks, points=points, snaps=snaps), warns


def _corner_candidates(g: np.ndarray, uv: np.ndarray, radius: float) -> list[tuple[np.ndarray, float]]:
    pad = 8
    x0, x1 = max(int(math.floor(uv[0] - radius)) - pad, 0), min(int(math.ceil(uv[0] + radius)) + pad + 1, g.shape[1])
    y0, y1 = max(int(math.floor(uv[1] - radius)) - pad, 0), min(int(math.ceil(uv[1] + radius)) + pad + 1, g.shape[0])
    patch = g[y0:y1, x0:x1]
    if patch.shape[0] < 7 or patch.shape[1] < 7:
        return []
    resp = cv2.cornerMinEigenVal(patch, blockSize=5, ksize=3)
    top = float(resp.max())
    if top <= 0:
        return []
    peak = (resp == cv2.dilate(resp, np.ones((5, 5), np.uint8))) & (resp > 0.05 * top)
    out = [(np.array([xx + x0 + 0.5, yy + y0 + 0.5]), float(resp[yy, xx])) for yy, xx in zip(*np.nonzero(peak))]
    out = [(p, s) for p, s in out if np.hypot(*(p - uv)) <= radius]
    return sorted(out, key=lambda t: -t[1])


def _corner_quality(g: np.ndarray, uv: np.ndarray, sigma: float = 2.0) -> float:
    """1 - exp(-(s / 4)^2) times the isotropy sqrt(min(1, 3 lmin / lmax)), with s the square root of the smallest
    structure-tensor eigenvalue (gradients as grey levels per pixel): a strong corner, not an edge, scores near 1."""
    half = int(math.ceil(3 * sigma)) + 2
    cu, cv_ = int(math.floor(uv[0])), int(math.floor(uv[1]))
    x0, x1 = max(cu - half, 0), min(cu + half + 1, g.shape[1])
    y0, y1 = max(cv_ - half, 0), min(cv_ + half + 1, g.shape[0])
    patch = g[y0:y1, x0:x1]
    if patch.shape[0] < 5 or patch.shape[1] < 5:
        return 0.0
    ix = cv2.Sobel(patch, cv2.CV_32F, 1, 0, ksize=3) / 8.0
    iy = cv2.Sobel(patch, cv2.CV_32F, 0, 1, ksize=3) / 8.0
    yy, xx = np.mgrid[y0:y1, x0:x1]
    w = np.exp(-(((xx + 0.5) - uv[0]) ** 2 + ((yy + 0.5) - uv[1]) ** 2) / (2 * sigma ** 2))
    w = w / w.sum()
    a, b, c = float((w * ix * ix).sum()), float((w * ix * iy).sum()), float((w * iy * iy).sum())
    disc = math.sqrt(max((a + c) ** 2 / 4 - (a * c - b * b), 0.0))
    lmin, lmax = max((a + c) / 2 - disc, 0.0), (a + c) / 2 + disc
    iso = math.sqrt(min(1.0, 3.0 * lmin / lmax)) if lmax > 0 else 0.0
    return (1.0 - math.exp(-(math.sqrt(lmin) / 4.0) ** 2)) * iso


def _segments_near(g: np.ndarray, uv: np.ndarray, radius: float, min_length: float) -> np.ndarray:
    span = int(math.ceil(3 * radius)) + 10
    x0, x1 = max(int(uv[0]) - span, 0), min(int(uv[0]) + span + 1, g.shape[1])
    y0, y1 = max(int(uv[1]) - span, 0), min(int(uv[1]) + span + 1, g.shape[0])
    patch = g[y0:y1, x0:x1].astype(np.uint8)
    if patch.shape[0] < 8 or patch.shape[1] < 8:
        return np.zeros((0, 4))
    segs = detect_segments(patch, min_length=min_length)
    return segs + np.array([x0, y0, x0, y0], dtype=float) if len(segs) else segs


def _point_segment_distance(p: np.ndarray, s: np.ndarray) -> float:
    a, ab = s[:2], s[2:] - s[:2]
    t = float(np.clip((p - a) @ ab / max(ab @ ab, 1e-12), 0.0, 1.0))
    return float(np.hypot(*(a + t * ab - p)))


def _junction(g: np.ndarray, uv: np.ndarray, radius: float) -> tuple[np.ndarray | None, float, str]:
    """The nearest well-supported meeting of two detected segments near uv that forms a real corner: both reach it
    (within 4 px or a quarter of their length) and cross at more than 20 degrees."""
    segs = _segments_near(g, uv, radius, 8.0)
    if len(segs) < 2:
        return None, 0.0, "fewer than two segments nearby"
    L = segment_lengths(segs)
    near = [i for i in range(len(segs)) if _point_segment_distance(uv, segs[i]) <= radius + 0.5 * L[i]]
    found = []
    for ii, i in enumerate(near):
        for j in near[ii + 1:]:
            l1 = np.cross([*segs[i][:2], 1.0], [*segs[i][2:], 1.0])
            l2 = np.cross([*segs[j][:2], 1.0], [*segs[j][2:], 1.0])
            x = np.cross(l1, l2)
            if abs(x[2]) < 1e-9:
                continue
            p = x[:2] / x[2]
            d1, d2 = segs[i][2:] - segs[i][:2], segs[j][2:] - segs[j][:2]
            sin = abs(d1[0] * d2[1] - d1[1] * d2[0]) / max(np.hypot(*d1) * np.hypot(*d2), 1e-12)
            if (sin < math.sin(math.radians(20.0)) or np.hypot(*(p - uv)) > radius or
                    _point_segment_distance(p, segs[i]) > max(4.0, 0.25 * L[i]) or
                    _point_segment_distance(p, segs[j]) > max(4.0, 0.25 * L[j])):
                continue
            found.append((p, math.sqrt(L[i] * L[j]) * sin))
    if not found:
        return None, 0.0, "no segment pair meets near the pick"
    # the nearest well-supported junction: two corners of a narrow mullion both score well, and the pick says which
    top = max(s for _, s in found)
    p, score = min(((p, s) for p, s in found if s >= 0.5 * top), key=lambda t: np.hypot(*(t[0] - uv)))
    return p, 1.0 - math.exp(-score / 20.0), f"segment junction score {score:.1f}"


def snap_corner(gray: np.ndarray, uv: Sequence[float], *, radius: float = 12.0,
                min_quality: float = 0.3) -> SnapResult:
    """Snap a pick to the nearest strong corner (subpixel), cross-checked against the meeting of detected segments.
    When the two disagree by more than 2 px the pick stays where it was: near a busy junction neither can be trusted,
    and moving the pick would hide that."""
    g = np.asarray(gray, dtype=np.float32)
    uv = np.asarray(uv, dtype=float)
    notes, corner, qc = [], None, 0.0
    cands = _corner_candidates(g, uv, radius)
    if cands:
        top = cands[0][1]
        k0 = min((k for k, c in enumerate(cands) if c[1] >= 0.3 * top), key=lambda k: np.hypot(*(cands[k][0] - uv)))
        p0, r0 = cands[k0]
        pts = to_cv(p0).reshape(1, 1, 2).astype(np.float32)
        cv2.cornerSubPix(g, pts, (5, 5), (-1, -1), (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 60, 1e-3))
        p = from_cv(pts.reshape(2).astype(float))
        if np.hypot(*(p - p0)) > 3.0 or np.hypot(*(p - uv)) > radius + 2.0:
            notes.append("corner refinement unstable")
        else:
            corner, qc = p, _corner_quality(g, p)
            notes.append(f"corner quality {qc:.2f}")
            rivals = [c for k, c in enumerate(cands) if k != k0 and c[1] > 0.6 * r0 and np.hypot(*(c[0] - p0)) > 4.0]
            if rivals:
                qc *= 0.5
                notes.append(f"ambiguous: {len(rivals)} comparable corner(s) in the radius")
    else:
        notes.append("no corner response")
    inter, qs, why = _junction(g, uv, radius)
    notes.append(why)
    stay = SnapResult((float(uv[0]), float(uv[1])), False, 0.0, "none", 0.0, "")
    if corner is not None and inter is not None:
        gap = float(np.hypot(*(corner - inter)))
        if gap > 2.0:
            notes.append(f"ambiguous: corner and segment junction disagree by {gap:.1f} px")
            return replace(stay, quality=0.5 * max(qc, qs), detail="; ".join(notes))
        px, q, method = inter, min(1.0, max(qc, qs) + 0.25 * min(qc, qs)), "corner+segments"
    elif corner is not None:
        px, q, method = corner, qc, "corner"
    elif inter is not None:
        px, q, method = inter, qs, "segments"
    else:
        return replace(stay, detail="; ".join(notes))
    if q < min_quality:
        notes.append(f"quality {q:.2f} below {min_quality}")
        return replace(stay, quality=float(q), method=method, detail="; ".join(notes))
    return SnapResult((float(px[0]), float(px[1])), True, float(q), method, float(np.hypot(*(px - uv))),
                      "; ".join(notes))


def snap_to_edge(gray: np.ndarray, uv: Sequence[float], *, radius: float = 10.0, min_length: float = 15.0,
                 min_quality: float = 0.3) -> SnapResult:
    """Move a pick perpendicular onto the nearest long detected segment; quality 1 - exp(-length / 40)."""
    g = np.asarray(gray, dtype=np.float32)
    uv = np.asarray(uv, dtype=float)
    best, best_d = None, float("inf")
    for s in _segments_near(g, uv, radius, min_length):
        a, d = s[:2], s[2:] - s[:2]
        L = float(np.hypot(*d))
        t = float((uv - a) @ d / (L * L))
        if L < min_length or t < -0.25 or t > 1.25:
            continue
        foot = a + t * d
        dist = float(np.hypot(*(foot - uv)))
        if dist <= radius and dist < best_d:
            best, best_d = (foot, L), dist
    if best is None:
        return SnapResult((float(uv[0]), float(uv[1])), False, 0.0, "none", 0.0, "no segment within the radius")
    foot, L = best
    q = 1.0 - math.exp(-L / 40.0)
    px = foot if q >= min_quality else uv
    return SnapResult((float(px[0]), float(px[1])), q >= min_quality, float(q), "edge", float(np.hypot(*(px - uv))),
                      f"segment length {L:.0f} px, {best_d:.1f} px from the pick")
