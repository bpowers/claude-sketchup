"""Feature references: a pick's model position looked up in house.json.

A pick names the model feature it was aimed at, and the position is read from house.json every time the picks are
loaded. A camera is then always solved against the model as it stands, and when an element moves, every pick on it
moves with it.

A reference is an object naming one element or opening by id, plus selectors. Each selector pins coordinates of a
point on that element. A list of references pins the union of their coordinates; a coordinate pinned twice must agree
to 0.02 in. A coordinate nobody pins is the free axis of a line pick: at most one, and the pick must declare it with
"free", so a forgotten selector fails instead of quietly turning a point into a line.

    wall       face   left | right | centre | +x | -x | +y | -y      across the wall (left is a->b turned +90 deg)
               end    a | b                                         along the wall
               level  base | top                                    z (a sloped top needs end a or b)
    opening    face   as for its wall                               across the host wall
               jamb   start (at) | end (at + width)                 along the host wall
               level  sill | head                                   z
               casing inner | outer   then face, jamb and level pick the edges of the casing on that face, on its
                               front plane (proud of the wall face): inner its edge round the opening, outer its
                               outside edge, as the planner sizes it (ir/plan.py, defaults included)
               light  [i, j]   then face, jamb and level pick the edges of the unit's clear light in column i and row
                               j, as the frame, mullions and rails split the unit, on the unit's faces (its depth,
                               default min(wall thickness, 3.5), centred offset from the wall's centreline)
    box        x, y, z   min | max | centre
    column     part   shaft | capital    (default shaft; pins nothing by itself)
               x, y   min | max | centre (min and max need a square section)
               level  base | top         (of the part)
    beam       side   as a wall's face                              across the beam
               end    a | b                                         along the beam
               level  soffit | top                                  z
    stair      riser  k      0 is the top riser, on the a->b nosing line; riser k lies k runs along descend
               level  nosing | foot   the top or the bottom of riser k
               end    a | b                                         along the nosing lines
    space      vertex i | corner "+x+y"   the outline vertex furthest that way              x and y
               x, y   min | max   an outline edge along a building axis at the outline's extreme  x or y
               level  floor | ceiling                               z (a ceiling_plane is a height, below)
    slab       vertex, corner, x, y as for space; level top | bottom
    roof_plane vertex i        pts[i], a corner of the top face                            x, y and z
               edge [i, j]     the top face's edge from pts[i] to pts[j] where it runs along a building axis: the
                               two coordinates it keeps (a line pick along the third)
               level top | bottom   top (the default) is the top face; bottom is thickness below it, the lower edge
                               of the vertical fascia. With no vertex or edge, a height (below)

Skewed elements. A wall, opening or beam that runs along no building axis pins, with each face, side, end or jamb, the
vertical plane through it. The planes and the other pins meet as far as they can: a plan point (a face and an end, or
two walls' faces), so the reference is a point or a vertical line as usual; or, where they leave one plan direction
and a level pins z, a horizontal line along the element (Resolved.direction), whose "free" names the axis it runs
nearest. A face word +x, -x, +y or -y names the face whose normal points within 45 degrees of it.

Heights. A sloped ceiling and a roof plane's top or bottom give z as a function of x and y, so they take z where the
rest of the reference pins x and y: [{"el": wall A, "face": "-y"}, {"el": wall B, "face": "+x"}, {"el": space,
"level": "ceiling"}] is where the corner of A and B meets a sloped ceiling. A plan coordinate nobody pins is allowed
only where the surface rises at most 0.02 in along it, since only then does a line along that axis lie on it.

Combinators:

    [ref, ref, ...]            every coordinate pinned by any of them (an inside corner: two walls' faces)
    {"between": [ref, ref]}    the midpoint of two references that pin the same axes (a mullion's centre)

A pick may add "offset": [dx, dy, dz] with an "offset_note" for a feature the model does not contain that sits a
known distance from one it does (crown moulding under a modelled ceiling). Explicit "model" coordinates, for things
not in the model at all, live in overlay/picks.py.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from shapely.geometry import Point, Polygon

AXES = "xyz"
AGREE_IN = 0.02          # two pins of one coordinate must agree this closely
AXIS_TOL = 1e-3          # |sin| of an element direction off a building axis
SKEW_WORD_MARGIN = 0.05  # a face word needs the normal's two plan components this far apart (not near 45 degrees)
UNIT_DEPTH_MAX = 3.5     # a unit's frame depth across its wall when the unit names none
SIDE_WORDS = ("left", "right", "centre", "+x", "-x", "+y", "-y")
EXTENT_WORDS = ("min", "max", "centre")
FRAME_SIDES = ("left", "right", "bottom", "top")

SELECTORS = {
    "wall": ("face", "end", "level"),
    "opening": ("face", "jamb", "level", "light", "casing"),
    "box": ("x", "y", "z"),
    "column": ("part", "x", "y", "level"),
    "beam": ("side", "end", "level"),
    "stair": ("riser", "level", "end"),
    "space": ("vertex", "corner", "x", "y", "level"),
    "slab": ("vertex", "corner", "x", "y", "level"),
    "roof_plane": ("vertex", "edge", "level"),
}


class FeatureError(ValueError):
    """A reference that cannot be resolved against house.json."""


@dataclass(frozen=True)
class Pin:
    value: float
    element: str
    what: str


@dataclass(frozen=True)
class _PlanPlane:
    """A vertical plane normal . (x, y) = value: a face, side, end or jamb of a skewed element. mid is the element's
    plan middle, where a line the planes leave is placed."""

    normal: tuple[float, float]
    value: float
    element: str
    what: str
    mid: tuple[float, float]


@dataclass(frozen=True)
class Height:
    """A z pin that depends on the plan point: the plane z = a x + b y + c over a plan polygon (None: everywhere);
    bounds (xmin, ymin, xmax, ymax) is the plan extent a line on it can span."""

    element: str
    what: str
    coef: tuple[float, float, float]
    polygon: Any
    bounds: tuple[float, float, float, float]

    def z_at(self, x: float, y: float) -> float | None:
        if self.polygon is not None and self.polygon.distance(Point(float(x), float(y))) > 1e-6:
            return None
        a, b, c = self.coef
        return a * x + b * y + c

    def rise(self, axis: int) -> float:
        """How far the surface rises along plan axis 0 (x) or 1 (y) across its bounds."""
        return abs(self.coef[axis]) * (self.bounds[axis + 2] - self.bounds[axis])


@dataclass(frozen=True)
class Resolved:
    """A resolved reference: coordinates with None on the free axis, and where they came from."""

    model: tuple[float | None, float | None, float | None]
    elements: tuple[str, ...]
    srcs: Mapping[str, str]
    description: str
    # a line along no building axis (a side of a skewed element): its unit direction; model is then a point on it
    direction: tuple[float, float, float] | None = None

    @property
    def free_axis(self) -> int | None:
        if self.direction is not None:
            return max(range(3), key=lambda k: abs(self.direction[k]))
        return next((i for i, v in enumerate(self.model) if v is None), None)


@dataclass(frozen=True)
class Surface:
    """A reference that names a surface (resolve_target): the plane axis = value where it pins one coordinate, or the
    Height it names."""

    elements: tuple[str, ...]
    description: str
    axis: int | None = None
    value: float | None = None
    height: Height | None = None


# ----------------------------------------------------------------------- house


class House:
    """Element and opening index over a house.json document. sha256 is of the file's bytes, the identity the camera
    JSON and SketchUp's HOUSE group record."""

    def __init__(self, doc: Mapping[str, Any], path: Path | None = None, sha256: str | None = None):
        if not isinstance(doc, Mapping) or not isinstance(doc.get("elements"), list):
            raise FeatureError(f"{path or 'house document'}: not a house.json document (no 'elements' list)")
        self.doc = doc
        self.path = path
        self.sha256 = sha256 or hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
        self.elements: dict[str, dict] = {}
        self.openings: dict[str, tuple[dict, dict]] = {}
        self._planned = None
        for el in doc["elements"]:
            if isinstance(el, dict) and isinstance(el.get("id"), str):
                self.elements[el["id"]] = el
                if el.get("type") == "wall":
                    for op in el.get("openings") or []:
                        if isinstance(op, dict) and isinstance(op.get("id"), str):
                            self.openings[op["id"]] = (el, op)

    @classmethod
    def load(cls, path: str | Path) -> "House":
        p = Path(path)
        raw = p.read_bytes()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise FeatureError(f"{p}: invalid JSON: {e}") from e
        return cls(doc, p.resolve(), hashlib.sha256(raw).hexdigest())

    def planned_opening(self, oid: str) -> dict:
        """The planner's record of an opening (ir/plan.py): its rect and casing, every default filled in."""
        if self._planned is None:
            from ir.plan import plan_document
            plan = plan_document(dict(self.doc), str(self.path or "the house document"))
            ops = {o["id"]: o for el in plan.elements if el.get("type") == "wall" for o in el.get("openings") or []}
            self._planned = (ops, plan)
        ops, plan = self._planned
        if oid not in ops:
            raise FeatureError(f"{oid}: the planner refuses its wall, so its casing has no position:\n"
                               f"{plan.message()}")
        return ops[oid]

    def lookup(self, eid: Any) -> tuple[str, dict, dict | None]:
        """(kind, element, host wall or None); kind is the element type, or 'opening'."""
        if not isinstance(eid, str) or not eid:
            raise FeatureError(f"'el' must be an element or opening id, got {eid!r}")
        if eid in self.elements:
            return self.elements[eid]["type"], self.elements[eid], None
        if eid in self.openings:
            wall, op = self.openings[eid]
            return "opening", op, wall
        near = difflib.get_close_matches(eid, list(self.elements) + list(self.openings), n=3, cutoff=0.6)
        hint = f"; did you mean {', '.join(near)}?" if near else ""
        raise FeatureError(f"'{eid}' is not an element or opening id in {self.path or 'house.json'}{hint}")

    def src_of(self, eid: str) -> str:
        if eid in self.elements:
            return str(self.elements[eid].get("src"))
        wall, op = self.openings[eid]
        return str(op.get("src", wall.get("src")))


# ------------------------------------------------------------------ plan frames


def _num(el: Mapping, key: str, eid: str) -> float:
    v = el.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise FeatureError(f"{eid}: field '{key}' must be a finite number, got {v!r}")
    return float(v)


def _pt2(el: Mapping, key: str, eid: str) -> tuple[float, float]:
    v = el.get(key)
    if not (isinstance(v, list) and len(v) == 2 and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                                                         for c in v)):
        raise FeatureError(f"{eid}: field '{key}' must be [x, y], got {v!r}")
    return float(v[0]), float(v[1])


def _pin(pins: dict[int, Pin], axis: int, value: float, eid: str, what: str) -> None:
    if not math.isfinite(value):
        raise FeatureError(f"{eid}: {what} is not finite")
    old = pins.get(axis)
    if old is not None and abs(old.value - value) > AGREE_IN:
        raise FeatureError(f"{AXES[axis]} is pinned twice and the pins disagree: {old.element} {old.what} gives "
                           f"{old.value:.3f}, {eid} {what} gives {value:.3f} ({abs(old.value - value):.3f} in apart)")
    if old is None:
        pins[axis] = Pin(value, eid, what)


@dataclass(frozen=True)
class _Frame:
    """An element's a -> b line in plan: u along it, n to its left."""

    a: tuple[float, float]
    u: tuple[float, float]
    n: tuple[float, float]
    length: float
    eid: str

    @classmethod
    def of(cls, el: Mapping, eid: str) -> "_Frame":
        a, b = _pt2(el, "a", eid), _pt2(el, "b", eid)
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < 1e-9:
            raise FeatureError(f"{eid}: a and b coincide")
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        return cls(a, (ux, uy), (-uy, ux), L, eid)

    @property
    def mid(self) -> tuple[float, float]:
        return self.a[0] + self.u[0] * self.length / 2.0, self.a[1] + self.u[1] * self.length / 2.0

    def axis(self, vec: tuple[float, float], what: str) -> tuple[int, int]:
        """(axis index, sign) of a plan direction that lies along a building axis."""
        vx, vy = vec
        if abs(vy) <= AXIS_TOL:
            return 0, 1 if vx > 0 else -1
        if abs(vx) <= AXIS_TOL:
            return 1, 1 if vy > 0 else -1
        raise FeatureError(f"{self.eid}: {what} runs at ({vx:.4f}, {vy:.4f}), not along a building axis")

    def dominant(self, vec: tuple[float, float], what: str) -> tuple[int, int]:
        """(axis index, sign) of the building axis a plan direction runs within 45 degrees of; refused too near 45."""
        vx, vy = vec
        if abs(vy) <= AXIS_TOL or abs(vx) <= AXIS_TOL:
            return self.axis(vec, what)
        if abs(abs(vx) - abs(vy)) < SKEW_WORD_MARGIN:
            raise FeatureError(f"{self.eid}: {what} runs at ({vx:.4f}, {vy:.4f}), too near 45 degrees to name it by a "
                               "building axis; use left or right")
        k = 0 if abs(vx) > abs(vy) else 1
        return k, 1 if vec[k] > 0 else -1

    def pin(self, pins: dict[int, Pin], planes: list[_PlanPlane], which: str, s: float, label: str) -> None:
        """Pin the point s along the element (which 'along') or s across it, left positive ('across'): one coordinate
        when that direction runs along a building axis, otherwise the vertical plane through the point."""
        vec = self.u if which == "along" else self.n
        px, py = self.a[0] + vec[0] * s, self.a[1] + vec[1] * s
        if abs(vec[0]) <= AXIS_TOL or abs(vec[1]) <= AXIS_TOL:
            k, _ = self.axis(vec, label)
            _pin(pins, k, (px, py)[k], self.eid, label)
        else:
            planes.append(_PlanPlane((vec[0], vec[1]), vec[0] * px + vec[1] * py, self.eid, label, self.mid))


def _side_offset(frame: _Frame, word: Any, half: float, key: str) -> float:
    """Offset from the centreline, left positive, of a face or side word."""
    if word not in SIDE_WORDS:
        raise FeatureError(f"{frame.eid}: {key} must be one of {', '.join(SIDE_WORDS)}, got {word!r}")
    if word == "centre":
        return 0.0
    if word in ("left", "right"):
        return half if word == "left" else -half
    k, sign = frame.dominant(frame.n, f"the {key} normal")
    if AXES.index(word[1]) != k:
        raise FeatureError(f"{frame.eid}: {key} {word} is not a face of this element; its faces point "
                           f"{'+-'[sign < 0]}{AXES[k]} (left) and {'-+'[sign < 0]}{AXES[k]} (right)")
    return half if (1 if word[0] == "+" else -1) == sign else -half


def _choose(sel: Mapping, key: str, allowed: Sequence[str], eid: str) -> str | None:
    v = sel.get(key)
    if v is not None and v not in allowed:
        raise FeatureError(f"{eid}: {key} must be one of {', '.join(map(str, allowed))}, got {v!r}")
    return v


def _extent(lo: float, hi: float, word: str) -> float:
    return lo if word == "min" else hi if word == "max" else 0.5 * (lo + hi)


# ------------------------------------------------------------------- resolvers


def _wall(el: dict, sel: Mapping, eid: str, planes: list[_PlanPlane]) -> dict[int, Pin]:
    fr = _Frame.of(el, eid)
    pins: dict[int, Pin] = {}
    if sel.get("face") is not None:
        fr.pin(pins, planes, "across", _side_offset(fr, sel["face"], _num(el, "thickness", eid) / 2.0, "face"),
               f"face {sel['face']}")
    end = _choose(sel, "end", ("a", "b"), eid)
    if end is not None:
        fr.pin(pins, planes, "along", 0.0 if end == "a" else fr.length, f"end {end}")
    level = _choose(sel, "level", ("base", "top"), eid)
    if level == "base":
        _pin(pins, 2, _num(el, "z0", eid), eid, "level base")
    elif level == "top":
        ta, tb = _num(el, "top_a", eid), _num(el, "top_b", eid)
        if end is None and abs(ta - tb) > AGREE_IN:
            raise FeatureError(f"{eid}: level top on a sloped wall (top_a {ta}, top_b {tb}) needs end a or b")
        _pin(pins, 2, tb if end == "b" else ta, eid, "level top")
    return pins


def frame_sides(op: Mapping) -> dict[str, float]:
    """A unit's four frame widths {left, right, bottom, top}. A number is the conventional frame: four sides for a
    window, three for a door or cased opening (no threshold member). Left is the jamb at the opening's start."""
    raw = (op.get("unit") or {}).get("frame")
    if isinstance(raw, Mapping):
        bad = [k for k in FRAME_SIDES
               if isinstance(raw.get(k), bool) or not isinstance(raw.get(k), (int, float)) or not math.isfinite(raw[k])]
        if bad:
            raise FeatureError(f"{op.get('id')}: a unit frame object needs numbers {', '.join(FRAME_SIDES)}; missing "
                               f"or not numbers: {', '.join(bad)}")
        return {k: float(raw[k]) for k in FRAME_SIDES}
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw)):
        raise FeatureError(f"{op.get('id')}: unit frame must be a number or an object {{left, right, bottom, top}}")
    w = 0.0 if raw is None else float(raw)
    return {"left": w, "right": w, "bottom": w if op.get("kind") == "window" else 0.0, "top": w}


def unit_grid(op: Mapping, s: float, e: float, sill: float,
              head: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(columns along the wall, rows in z) of a unit's clear lights: [s, e] x [sill, head] less the frame sides, split
    by the mullions at s + verticals and the rails at horizontals."""
    u = op.get("unit") or {}
    side = frame_sides(op)

    def split(lo: float, hi: float, centres: Sequence[float], w: float) -> list[tuple[float, float]]:
        out, cur = [], lo
        for c in centres:
            out.append((cur, c - w / 2.0))
            cur = c + w / 2.0
        out.append((cur, hi))
        return out

    cols = split(s + side["left"], e - side["right"], [s + float(v) for v in u.get("verticals") or []],
                 float(u.get("mullion") or 0.0))
    rows = split(sill + side["bottom"], head - side["top"], [float(v) for v in u.get("horizontals") or []],
                 float(u.get("rail") or 0.0))
    return cols, rows


def unit_plane(wall: Mapping, op: Mapping) -> tuple[float, float]:
    """(offset from the centreline toward the left face, depth) of an opening unit's frame: depth min(thickness,
    UNIT_DEPTH_MAX) unless the unit names one, offset 0 unless named, both kept inside the wall."""
    t = float(wall["thickness"])
    u = op.get("unit") or {}
    depth = min(float(u["depth"]) if isinstance(u.get("depth"), (int, float)) else min(t, UNIT_DEPTH_MAX), t)
    room = (t - depth) / 2.0
    off = float(u["offset"]) if isinstance(u.get("offset"), (int, float)) else 0.0
    return max(-room, min(room, off)), depth


def _opening(house: House, op: dict, wall: dict, sel: Mapping, eid: str,
             planes: list[_PlanPlane]) -> dict[int, Pin]:
    fr = _Frame.of(wall, wall["id"])
    t = _num(wall, "thickness", wall["id"])
    at, width = _num(op, "at", eid), _num(op, "width", eid)
    sill, head = _num(op, "sill_z", eid), _num(op, "head_z", eid)
    light = sel.get("light")
    casing = _choose(sel, "casing", ("inner", "outer"), eid)
    centre = 0.0
    if casing is not None:
        if light is not None:
            raise FeatureError(f"{eid}: casing and light name different parts; give one")
        if sel.get("face") in (None, "centre"):
            raise FeatureError(f"{eid}: casing needs the face it is on (left, right, or a face word such as +y)")
        planned = house.planned_opening(eid)
        c = planned.get("casing")
        side = _side_offset(_Frame(fr.a, fr.u, fr.n, fr.length, eid), sel["face"], 1.0, "face")
        face = "left" if side > 0 else "right"
        if not c or face not in c["faces"]:
            raise FeatureError(f"{eid}: no casing on its {face} face" +
                               (f" (it has one on {', '.join(c['faces'])})" if c else ""))
        s0, s1, z0, z1 = c["outer"] if casing == "outer" else planned["rect"]
        span, band, what = (s0, s1), (z0, z1), f"casing {casing} "
        centre, half = side * (t / 2.0 + c["proud"]), 0.0
    elif light is None:
        span, band, half, what = (at, at + width), (sill, head), t / 2.0, ""
    else:
        if not isinstance(op.get("unit"), Mapping):
            raise FeatureError(f"{eid}: light {light!r}, but the opening has no unit")
        if not (isinstance(light, list) and len(light) == 2 and
                all(isinstance(v, int) and not isinstance(v, bool) for v in light)):
            raise FeatureError(f"{eid}: light must be [column, row] integers, got {light!r}")
        cols, rows = unit_grid(op, at, at + width, sill, head)
        i, j = light
        if not (0 <= i < len(cols) and 0 <= j < len(rows)):
            raise FeatureError(f"{eid}: light [{i}, {j}] does not exist; the unit has {len(cols)} column(s) and "
                               f"{len(rows)} row(s) of lights")
        centre, depth = unit_plane(wall, op)
        span, band, half, what = cols[i], rows[j], depth / 2.0, f"light [{i}, {j}] "
    pins: dict[int, Pin] = {}
    fr = _Frame(fr.a, fr.u, fr.n, fr.length, eid)        # pins name the opening, not its wall
    if sel.get("face") is not None:
        across = centre if casing is not None else centre + _side_offset(fr, sel["face"], half, "face")
        fr.pin(pins, planes, "across", across, f"{what}face {sel['face']}")
    jamb = _choose(sel, "jamb", ("start", "end"), eid)
    if jamb is not None:
        fr.pin(pins, planes, "along", span[0] if jamb == "start" else span[1], f"{what}jamb {jamb}")
    level = _choose(sel, "level", ("sill", "head"), eid)
    if level is not None:
        _pin(pins, 2, band[0] if level == "sill" else band[1], eid, f"{what}level {level}")
    return pins


def _box(el: dict, sel: Mapping, eid: str) -> dict[int, Pin]:
    bb = el.get("bbox")
    if not (isinstance(bb, list) and len(bb) == 6 and all(isinstance(v, (int, float)) for v in bb)):
        raise FeatureError(f"{eid}: bbox must be [x0, y0, z0, x1, y1, z1], got {bb!r}")
    pins: dict[int, Pin] = {}
    for k, key in enumerate(AXES):
        word = _choose(sel, key, EXTENT_WORDS, eid)
        if word is not None:
            _pin(pins, k, _extent(float(bb[k]), float(bb[k + 3]), word), eid, f"{key} {word}")
    return pins


def _column(el: dict, sel: Mapping, eid: str) -> dict[int, Pin]:
    cx, cy = _pt2(el, "center", eid)
    z0, z1 = _num(el, "z0", eid), _num(el, "z1", eid)
    cap = el.get("capital")
    part = _choose(sel, "part", ("shaft", "capital"), eid) or "shaft"
    square = el.get("shape") == "square"
    if part == "capital":
        if not isinstance(cap, dict):
            raise FeatureError(f"{eid}: part capital, but the column has no capital")
        size, lo_z, hi_z, square = cap.get("size"), z1 - _num(cap, "height", eid), z1, True
    else:
        size, lo_z = el.get("size"), z0
        hi_z = z1 - _num(cap, "height", eid) if isinstance(cap, dict) else z1
    pins: dict[int, Pin] = {}
    for k, key in enumerate("xy"):
        word = _choose(sel, key, EXTENT_WORDS, eid)
        if word is None:
            continue
        c = (cx, cy)[k]
        if word == "centre":
            _pin(pins, k, c, eid, f"{part} {key} centre")
            continue
        if not square or not (isinstance(size, list) and len(size) == 2):
            raise FeatureError(f"{eid}: {key} {word} needs a square section; a round column's silhouette depends on "
                               "the viewpoint (use centre)")
        half = float(size[k]) / 2.0
        _pin(pins, k, c - half if word == "min" else c + half, eid, f"{part} {key} {word}")
    level = _choose(sel, "level", ("base", "top"), eid)
    if level is not None:
        _pin(pins, 2, lo_z if level == "base" else hi_z, eid, f"{part} level {level}")
    return pins


def _beam(el: dict, sel: Mapping, eid: str, planes: list[_PlanPlane]) -> dict[int, Pin]:
    fr = _Frame.of(el, eid)
    pins: dict[int, Pin] = {}
    if sel.get("side") is not None:
        fr.pin(pins, planes, "across", _side_offset(fr, sel["side"], _num(el, "width", eid) / 2.0, "side"),
               f"side {sel['side']}")
    end = _choose(sel, "end", ("a", "b"), eid)
    if end is not None:
        fr.pin(pins, planes, "along", 0.0 if end == "a" else fr.length, f"end {end}")
    level = _choose(sel, "level", ("soffit", "top"), eid)
    if level is not None:
        soffit = _num(el, "soffit_z", eid)
        _pin(pins, 2, soffit if level == "soffit" else soffit + _num(el, "depth", eid), eid, f"level {level}")
    return pins


def _stair(el: dict, sel: Mapping, eid: str) -> dict[int, Pin]:
    fr = _Frame.of(el, eid)
    d = _pt2(el, "descend", eid)
    risers = el.get("risers")
    if isinstance(risers, bool) or not isinstance(risers, (int, float)) or risers < 1 or risers != int(risers):
        raise FeatureError(f"{eid}: risers must be a positive integer, got {risers!r}")
    risers = int(risers)
    rise, run, zt = _num(el, "rise", eid), _num(el, "run", eid), _num(el, "z_top", eid)
    pins: dict[int, Pin] = {}
    k_riser = sel.get("riser")
    level = _choose(sel, "level", ("nosing", "foot"), eid)
    if k_riser is not None:
        if isinstance(k_riser, bool) or not isinstance(k_riser, int) or not 0 <= k_riser < risers:
            raise FeatureError(f"{eid}: riser must be an integer in 0..{risers - 1}, got {k_riser!r}")
        k, sign = fr.axis(d, "descend")
        _pin(pins, k, fr.a[k] + sign * k_riser * run, eid, f"riser {k_riser}")
    if level is not None:
        if k_riser is None:
            raise FeatureError(f"{eid}: level {level} needs riser k")
        _pin(pins, 2, zt - (k_riser + (1 if level == "foot" else 0)) * rise, eid, f"riser {k_riser} {level}")
    end = _choose(sel, "end", ("a", "b"), eid)
    if end is not None:
        k, _ = fr.axis(fr.u, "the nosing line")
        _pin(pins, k, fr.a[k] + fr.u[k] * (0.0 if end == "a" else fr.length), eid, f"end {end}")
    return pins


def _outline(el: dict, sel: Mapping, eid: str, pins: dict[int, Pin]) -> list:
    """Pin a space's or slab's vertex, corner or axis-aligned outline edge; returns the outline."""
    pts = el.get("outline")
    if not (isinstance(pts, list) and len(pts) >= 3):
        raise FeatureError(f"{eid}: outline must be a list of at least 3 [x, y] points")
    if "vertex" in sel and "corner" in sel:
        raise FeatureError(f"{eid}: give vertex or corner, not both")
    if "vertex" in sel:
        i = sel["vertex"]
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(pts):
            raise FeatureError(f"{eid}: vertex must be an integer in 0..{len(pts) - 1}, got {i!r}")
        for k in (0, 1):
            _pin(pins, k, float(pts[i][k]), eid, f"vertex {i}")
    if "corner" in sel:
        word = sel["corner"]
        if not (isinstance(word, str) and len(word) == 4 and word[0] in "+-" and word[1] == "x"
                and word[2] in "+-" and word[3] == "y"):
            raise FeatureError(f"{eid}: corner must look like +x+y or -x+y, got {word!r}")
        sx, sy = (1.0 if word[0] == "+" else -1.0), (1.0 if word[2] == "+" else -1.0)
        scores = sorted(((sx * float(p[0]) + sy * float(p[1]), j) for j, p in enumerate(pts)), reverse=True)
        if scores[0][0] - scores[1][0] < 0.5:
            raise FeatureError(f"{eid}: corner {word} is ambiguous (vertices {scores[0][1]} and {scores[1][1]} are "
                               "within 0.5 in of furthest that way); use vertex i")
        j = scores[0][1]
        for k in (0, 1):
            _pin(pins, k, float(pts[j][k]), eid, f"corner {word} (vertex {j})")
    for k, key in enumerate("xy"):
        word = _choose(sel, key, ("min", "max"), eid)
        if word is None:
            continue
        vals = [float(p[k]) for p in pts]
        ext = min(vals) if word == "min" else max(vals)
        if sum(abs(v - ext) < 0.5 for v in vals) < 2:
            raise FeatureError(f"{eid}: {key} {word}: only one outline vertex lies at {key} {ext:.2f}, so no outline "
                               "edge runs along a building axis there; use vertex i or corner")
        _pin(pins, k, ext, eid, f"outline {key} {word}")
    return pts


def _bounds(pts: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    xs, ys = [float(p[0]) for p in pts], [float(p[1]) for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _space(el: dict, sel: Mapping, eid: str, heights: list[Height]) -> dict[int, Pin]:
    pins: dict[int, Pin] = {}
    pts = _outline(el, sel, eid, pins)
    level = _choose(sel, "level", ("floor", "ceiling"), eid)
    if level == "floor":
        _pin(pins, 2, _num(el, "floor_z", eid), eid, "level floor")
    elif level == "ceiling" and el.get("ceiling_plane") is not None:
        cp = el["ceiling_plane"]
        try:
            (ax, ay, az), (sx, sy) = cp["at"], cp["slope"]
            coef = (float(sx), float(sy), float(az) - float(sx) * float(ax) - float(sy) * float(ay))
        except (KeyError, TypeError, ValueError):
            raise FeatureError(f"{eid}: ceiling_plane must be {{\"at\": [x, y, z], \"slope\": [dz/dx, dz/dy]}}, got "
                               f"{cp!r}") from None
        heights.append(Height(eid, "level ceiling (sloped)", coef, None, _bounds(pts)))
    elif level == "ceiling":
        if el.get("ceiling_z") is None:
            raise FeatureError(f"{eid}: level ceiling, but the space has no ceiling_z or ceiling_plane")
        _pin(pins, 2, _num(el, "ceiling_z", eid), eid, "level ceiling")
    return pins


def _slab(el: dict, sel: Mapping, eid: str) -> dict[int, Pin]:
    pins: dict[int, Pin] = {}
    _outline(el, sel, eid, pins)
    level = _choose(sel, "level", ("top", "bottom"), eid)
    if level is not None:
        zt = _num(el, "z_top", eid)
        _pin(pins, 2, zt if level == "top" else zt - _num(el, "thickness", eid), eid, f"level {level}")
    return pins


def _roof_plane(el: dict, sel: Mapping, eid: str, heights: list[Height]) -> dict[int, Pin]:
    pts = el.get("pts")
    if not (isinstance(pts, list) and len(pts) >= 3 and all(isinstance(p, list) and len(p) == 3 for p in pts)):
        raise FeatureError(f"{eid}: pts must be a list of at least 3 [x, y, z] points")
    if "vertex" in sel and "edge" in sel:
        raise FeatureError(f"{eid}: give vertex or edge, not both")
    level = _choose(sel, "level", ("top", "bottom"), eid) or "top"
    drop = 0.0 if level == "top" else _num(el, "thickness", eid)
    n = len(pts)
    pins: dict[int, Pin] = {}
    if "vertex" in sel:
        i = sel["vertex"]
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n:
            raise FeatureError(f"{eid}: vertex must be an integer in 0..{n - 1}, got {i!r}")
        what = f"vertex {i}" + (" bottom" if drop else "")
        for k in range(3):
            _pin(pins, k, float(pts[i][k]) - (drop if k == 2 else 0.0), eid, what)
    elif "edge" in sel:
        e = sel["edge"]
        if not (isinstance(e, list) and len(e) == 2 and
                all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n for v in e) and e[0] != e[1]):
            raise FeatureError(f"{eid}: edge must be two different vertex indices in 0..{n - 1}, got {e!r}")
        p, q = ([float(c) for c in pts[v]] for v in e)
        d = [q[k] - p[k] for k in range(3)]
        length = math.sqrt(sum(c * c for c in d))
        along = [k for k in range(3) if abs(d[k]) > AXIS_TOL * length]
        if len(along) != 1:
            raise FeatureError(f"{eid}: edge {e} runs at ({d[0]:.3f}, {d[1]:.3f}, {d[2]:.3f}), not along a building "
                               "axis, so no line pick describes it; use vertex i")
        for k in range(3):
            if k != along[0]:
                _pin(pins, k, 0.5 * (p[k] + q[k]) - (drop if k == 2 else 0.0), eid,
                     f"edge {e}" + (" bottom" if drop else ""))
    elif "level" in sel:
        A = np.array([[p[0], p[1], 1.0] for p in pts], dtype=float)
        coef, *_ = np.linalg.lstsq(A, np.array([p[2] for p in pts], dtype=float), rcond=None)
        poly = Polygon([(p[0], p[1]) for p in pts])
        heights.append(Height(eid, f"level {level}", (float(coef[0]), float(coef[1]), float(coef[2]) - drop), poly,
                              _bounds(pts)))
    return pins


def _settle(pins: dict[int, Pin], heights: Sequence[Height]) -> None:
    """Pin z from each height where the pins put x and y; a plan coordinate nothing pins is taken where a line along
    that axis meets the surface (allowed only where the surface is level along it)."""
    for h in heights:
        known = {k: pins[k].value for k in (0, 1) if k in pins}
        for k in (0, 1):
            if k not in known and h.rise(k) > AGREE_IN:
                raise FeatureError(f"{h.element}: {h.what} rises {h.rise(k):.2f} in along {AXES[k]}, so no line along "
                                   f"{AXES[k]} lies on it; pin {AXES[k]} too (a vertex, a corner or another reference)")
        axes = [[known[k]] if k in known else
                [0.5 * (h.bounds[k] + h.bounds[k + 2])] + list(np.linspace(h.bounds[k], h.bounds[k + 2], 17))
                for k in (0, 1)]
        z = next((z for z in (h.z_at(x, y) for x in axes[0] for y in axes[1]) if z is not None), None)
        over = ", ".join(f"{AXES[k]} {v:.2f}" for k, v in sorted(known.items())) or "anywhere"
        if z is None:
            raise FeatureError(f"{h.element}: {h.what}: no face of the element stands over {over}")
        _pin(pins, 2, z, h.element, f"{h.what} over {over}")


def _finish(pins: dict[int, Pin], heights: Sequence[Height], planes: Sequence[_PlanPlane]
            ) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Settle a reference's pins, heights and skewed planes. Planes that meet the plan pins in one plan point pin x
    and y there (None is returned: a point or an axis line). Parallel planes with nothing else in plan leave a line in
    plan; with z pinned it is a horizontal line, returned as (its point nearest the elements' middles, its
    direction)."""
    if not planes:
        _settle(pins, heights)
        return None
    rows = [((1.0, 0.0) if k == 0 else (0.0, 1.0), p.value, f"{p.element} {p.what}") for k, p in sorted(pins.items())
            if k < 2]
    rows += [(pl.normal, pl.value, f"{pl.element} {pl.what}") for pl in planes]
    N = np.array([r[0] for r in rows], dtype=float)
    c = np.array([r[1] for r in rows], dtype=float)
    sv = np.linalg.svd(N, compute_uv=False)
    what = " & ".join(f"{pl.element} {pl.what}" for pl in planes)
    if len(sv) == 2 and sv[1] > 1e-6 * sv[0]:
        known = [k for k in (0, 1) if k in pins]
        if len(known) == 1:
            k = known[0]
            j = 1 - k
            xy = np.zeros(2)
            xy[k] = pins[k].value
            xy[j] = float(np.mean([(pl.value - pl.normal[k] * xy[k]) / pl.normal[j] for pl in planes]))
        else:
            xy = np.linalg.lstsq(N, c, rcond=None)[0]
        off = np.abs(N @ xy - c)
        if off.max() > AGREE_IN:
            i = int(np.argmax(off))
            raise FeatureError(f"the plan pins do not meet in one point: {rows[i][2]} passes {off[i]:.3f} in from "
                               f"where {', '.join(r[2] for j, r in enumerate(rows) if j != i)} meet")
        for k in (0, 1):
            if k not in pins:
                _pin(pins, k, float(xy[k]), planes[0].element, what)
        _settle(pins, heights)
        return None
    n = np.array(planes[0].normal, dtype=float)
    for pl in planes[1:]:
        sign = 1.0 if np.dot(pl.normal, n) > 0 else -1.0
        if abs(sign * pl.value - planes[0].value) > AGREE_IN:
            raise FeatureError(f"{pl.element} {pl.what} and {planes[0].element} {planes[0].what} are parallel planes "
                               f"{abs(sign * pl.value - planes[0].value):.3f} in apart")
    t = np.array([-n[1], n[0]])
    q = planes[0].value * n + float(t @ np.mean([pl.mid for pl in planes], axis=0)) * t
    for h in heights:
        span = math.hypot(h.bounds[2] - h.bounds[0], h.bounds[3] - h.bounds[1])
        if abs(h.coef[0] * t[0] + h.coef[1] * t[1]) * span > AGREE_IN:
            raise FeatureError(f"{h.element}: {h.what} rises along {what}, so no line along it lies on the surface; "
                               "pin a plan coordinate too")
        z = next((z for z in (h.z_at(*(q + s * t)) for s in [0.0] + list(np.linspace(-span, span, 33)))
                  if z is not None), None)
        if z is None:
            raise FeatureError(f"{h.element}: {h.what}: no face of the element stands along {what}")
        _pin(pins, 2, z, h.element, f"{h.what} along {what}")
    if 2 not in pins:
        raise FeatureError(f"{what} is a vertical plane that runs along no building axis; pin z (a level) or another "
                           "plan coordinate too")
    return (float(q[0]), float(q[1]), float(pins[2].value)), (float(t[0]), float(t[1]), 0.0)


def _describe(sel: Mapping) -> str:
    parts = [f"{k} {sel[k]}" for k in sel if k != "el"]
    return f"{sel['el']}" + (f" [{', '.join(parts)}]" if parts else "")


def _resolve(house: House, ref: Any, used: list[str], path: str
             ) -> tuple[dict[int, Pin], list[Height], list[_PlanPlane], str]:
    """(pins, heights not yet settled, skewed planes not yet met, description) of a reference."""
    if isinstance(ref, list):
        if not ref:
            raise FeatureError(f"{path}: an empty reference list pins nothing")
        pins: dict[int, Pin] = {}
        heights: list[Height] = []
        planes: list[_PlanPlane] = []
        descs = []
        for i, r in enumerate(ref):
            sub, sub_heights, sub_planes, desc = _resolve(house, r, used, f"{path}[{i}]")
            for k, p in sub.items():
                _pin(pins, k, p.value, p.element, p.what)
            heights += sub_heights
            planes += sub_planes
            descs.append(desc)
        return pins, heights, planes, " & ".join(descs)
    if not isinstance(ref, dict):
        raise FeatureError(f"{path}: a reference must be an object or a list of them, got {ref!r}")
    if "between" in ref:
        if set(ref) != {"between"} or not (isinstance(ref["between"], list) and len(ref["between"]) == 2):
            raise FeatureError(f"{path}: between takes exactly two references and nothing else")
        sides = [_resolve(house, r, used, f"{path}.between[{i}]") for i, r in enumerate(ref["between"])]
        for p, h, q, d in sides:
            if _finish(p, h, q) is not None:
                raise FeatureError(f"{path}: between takes points and axis lines; {d} is a line along no building axis")
        (p0, _, _, d0), (p1, _, _, d1) = sides
        if set(p0) != set(p1):
            raise FeatureError(f"{path}: between needs references pinning the same axes, got "
                               f"{''.join(AXES[k] for k in sorted(p0))} and {''.join(AXES[k] for k in sorted(p1))}")
        mid = {k: Pin(0.5 * (p0[k].value + p1[k].value), p0[k].element, f"midpoint to {p1[k].element}") for k in p0}
        return mid, [], [], f"midway between {d0} and {d1}"
    eid = ref.get("el")
    kind, el, wall = house.lookup(eid)
    allowed = SELECTORS.get(kind)
    if allowed is None:
        raise FeatureError(f"{path}: {eid} is a {kind}; references to {kind} elements are not supported")
    unknown = set(ref) - {"el", *allowed}
    if unknown:
        raise FeatureError(f"{path}: {eid} is a {kind}, whose selectors are {', '.join(allowed)}; unknown "
                           f"{', '.join(sorted(unknown))}")
    used.append(eid)
    heights, planes = [], []
    if kind == "opening":
        pins = _opening(house, el, wall, ref, eid, planes)
    elif kind == "space":
        pins = _space(el, ref, eid, heights)
    elif kind == "roof_plane":
        pins = _roof_plane(el, ref, eid, heights)
    elif kind in ("wall", "beam"):
        pins = (_wall if kind == "wall" else _beam)(el, ref, eid, planes)
    else:
        pins = {"box": _box, "column": _column, "stair": _stair, "slab": _slab}[kind](el, ref, eid)
    if not pins and not heights and not planes:
        raise FeatureError(f"{path}: {_describe(ref)} pins no coordinate; add a selector ({', '.join(allowed)})")
    return pins, heights, planes, _describe(ref)


def _check_offset(offset: Any, where: str) -> list[float]:
    if not (isinstance(offset, (list, tuple)) and len(offset) == 3 and
            all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in offset)):
        raise FeatureError(f"{where}: offset must be [dx, dy, dz], got {offset!r}")
    return [float(v) for v in offset]


def resolve(house: House, ref: Any, *, free: str | None = None, offset: Sequence[float] | None = None,
            where: str = "reference") -> Resolved:
    """Resolve a reference to model coordinates (the module doc)."""
    used: list[str] = []
    try:
        pins, heights, planes, desc = _resolve(house, ref, used, "ref")
        line = _finish(pins, heights, planes)
    except FeatureError as e:
        raise FeatureError(f"{where}: {e}") from None
    seen = tuple(dict.fromkeys(used))
    srcs = {e: house.src_of(e) for e in seen}
    off = None if offset is None else _check_offset(offset, where)
    if line is not None:
        point, direction = line
        k = max(range(3), key=lambda i: abs(direction[i]))
        words = f"({direction[0]:.4f}, {direction[1]:.4f}, {direction[2]:.4f})"
        if free != AXES[k]:
            raise FeatureError(f"{where}: the reference is a line along {words}, a side of a skewed element, nearest "
                               f"{AXES[k]}; declare \"free\": \"{AXES[k]}\"")
        model = list(point)
        if off is not None:
            if abs(sum(o * d for o, d in zip(off, direction))) > 1e-9:
                raise FeatureError(f"{where}: offset moves the line along itself ({words}), which means nothing")
            model = [m + o for m, o in zip(model, off)]
        return Resolved(tuple(model), seen, srcs, desc, direction=direction)
    unpinned = [k for k in range(3) if k not in pins]
    if len(unpinned) > 1:
        raise FeatureError(f"{where}: the reference pins only {''.join(AXES[k] for k in sorted(pins)) or 'nothing'}; a "
                           "pick on a model plane constrains nothing. Pin all but one coordinate")
    if free is not None and free not in AXES:
        raise FeatureError(f"{where}: free must be x, y or z, got {free!r}")
    if unpinned and free != AXES[unpinned[0]]:
        raise FeatureError(f"{where}: the reference leaves {AXES[unpinned[0]]} unpinned, but free is {free!r}; declare "
                           f"\"free\": \"{AXES[unpinned[0]]}\" or pin it")
    if not unpinned and free is not None:
        p = pins[AXES.index(free)]
        raise FeatureError(f"{where}: free is {free!r}, but the reference pins {free} ({p.element} {p.what})")
    model = [pins[k].value if k in pins else None for k in range(3)]
    if off is not None:
        for k in range(3):
            if model[k] is None and off[k] != 0:
                raise FeatureError(f"{where}: offset moves the free axis {AXES[k]}, which means nothing")
            if model[k] is not None:
                model[k] += off[k]
    return Resolved(tuple(model), seen, srcs, desc)


def resolve_target(house: House, ref: Any, *, where: str = "reference") -> Resolved | Surface:
    """What a reference names, for measuring (overlay/measure.py): a Surface when it pins one coordinate and no
    height (the plane axis = value), or a height and nothing else (a sloped ceiling, a roof plane's top); otherwise
    the point or line, its free axis whichever coordinate it leaves unpinned."""
    used: list[str] = []
    try:
        pins, heights, planes, desc = _resolve(house, ref, used, "ref")
        seen = tuple(dict.fromkeys(used))
        if not heights and not planes and len(pins) == 1:
            (k, pin), = pins.items()
            return Surface(seen, desc, axis=k, value=pin.value)
        if not pins and not planes and len(heights) == 1:
            return Surface(seen, desc, height=heights[0])
        line = _finish(pins, heights, planes)
    except FeatureError as e:
        raise FeatureError(f"{where}: {e}") from None
    srcs = {e: house.src_of(e) for e in seen}
    if line is not None:
        return Resolved(line[0], seen, srcs, desc, direction=line[1])
    if len(pins) < 2:
        raise FeatureError(f"{where}: the reference pins only {''.join(AXES[k] for k in sorted(pins)) or 'nothing'}, "
                           "which names no point, line or single surface")
    return Resolved(tuple(pins[k].value if k in pins else None for k in range(3)), seen, srcs, desc)
