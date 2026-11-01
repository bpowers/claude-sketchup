"""The planner: everything a house document means, decided before anything reaches SketchUp.

plan_document(doc) checks a house.json document (docs/IR.md) element by element and returns a Plan:
- elements: each element as build.rb draws it, in document order: its own fields with every default filled in, and
  what the planner resolves for it (a wall's length, each opening's rectangle, a unit's frame sides and lights, a
  casing's outline). Each is plain JSON, with "id", "type", "zone" and "src" first.
- refusals: what build.rb could not draw, one Finding per refused element, naming it. A document with none builds.
- warnings: what builds but is probably not meant: an unknown finish (drawn as `placeholder`), an unknown field, a
  door sill on no floor, a sliver of wall SketchUp may not draw.

write_plan() writes the plan file build.rb reads. There is no other validator: this module is the gate, so every rule
build.rb would trip over is a refusal here, where it is tested without SketchUp.

    uv run python -m ir.plan house.json                 # refusals and warnings; exit 1 on any refusal
    uv run python -m ir.plan house.json -o plan.json    # and write the plan
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ir import geom
from ir.geom import EPS

ROOT = Path(__file__).resolve().parent.parent
PALETTE_PATH = ROOT / "build" / "palette.json"

IR_VERSION = 1
PLAN_VERSION = 1

TYPES = ("space", "slab", "wall", "column", "beam", "stair", "box", "roof_plane")
SRCS = ("cad", "scan", "photo", "owner", "measured", "assumed")
WALL_KINDS = ("exterior", "interior", "half_wall", "glass_wall", "masonry")
OPENING_KINDS = ("window", "door", "cased", "pass")
BEAM_KINDS = ("structural", "faux")
BOX_KINDS = ("base_cabinet", "upper_cabinet", "tall_cabinet", "counter", "appliance", "fixture", "soffit", "masonry",
             "hearth", "trim", "placeholder")
FRONTS = ("+x", "-x", "+y", "-y")
SHAPES = ("square", "round")
SIDES = ("left", "right", "bottom", "top")
HINGES = ("start", "end", "both")
FACES = ("both", "left", "right")
LOCATION_KEYS = ("latitude", "longitude", "north_angle", "city", "country", "tz_offset")

ID_RE = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)+")
ZONE_RE = re.compile(r"[a-z0-9_]+")

# The finish an element takes when it names none. Every value is a key of build/palette.json.
DEFAULT_FINISH = {
    "wall": {"exterior": "siding", "interior": "drywall", "half_wall": "drywall", "glass_wall": "drywall",
             "masonry": "brick"},
    "box": {"base_cabinet": "cabinet_wood", "upper_cabinet": "cabinet_wood", "tall_cabinet": "cabinet_wood",
            "counter": "counter_stone", "appliance": "stainless", "fixture": "porcelain", "soffit": "drywall",
            "masonry": "brick", "hearth": "brick", "trim": "trim_white", "placeholder": "placeholder"},
    "beam": {"structural": "wood_dark", "faux": "wood_dark"},
    "column": "paint_white", "stair": "wood_mid", "slab": "concrete", "roof_plane": "roof",
    "floor": "oak", "ceiling": "drywall",
    "unit": "trim_white",       # a unit's frame, mullions and rails
    "leaf": "wood_mid",         # an unglazed unit's leaf panels
    "casing": "trim_white",
}
UNIT_DEPTH_MAX = 3.5            # in: a unit frame's depth across its wall when it names none (thinner walls: theirs)
CASING_WIDTH = 3.5              # in: a casing's width when it names none
CASING_PROUD = 0.75             # in: how far a casing stands off the wall face when it names none
SILL_TOL = 0.5                  # in: a door's sill this near a floor beside it (or its wall's base) stands on it
SILL_PROBES = (1.0, 3.0, 6.0, 12.0)   # in: how far past each face of a wall the sill check looks for a floor
SLIVER = 0.05                   # in: a pier, sill or lintel thinner than this may merge away in SketchUp
ROUND_SIDES = 24                # the sides build.rb draws a round column with (ROUND_SEGMENTS)

COMMON = ("id", "type", "zone", "src", "name", "tags", "note")
KNOWN = {
    "space": ("outline", "floor_z", "ceiling_z", "ceiling_plane", "floor_finish", "ceiling_finish", "build_floor",
              "build_ceiling"),
    "slab": ("outline", "z_top", "thickness", "finish"),
    "wall": ("a", "b", "thickness", "z0", "top_a", "top_b", "kind", "finish", "finish_left", "finish_right",
             "openings"),
    "column": ("center", "shape", "size", "diameter", "z0", "z1", "capital", "finish"),
    "beam": ("a", "b", "width", "depth", "soffit_z", "kind", "finish"),
    "stair": ("a", "b", "descend", "z_top", "risers", "rise", "run", "finish"),
    "box": ("bbox", "kind", "subtype", "front", "seams", "rails", "finish"),
    "roof_plane": ("pts", "thickness", "holes", "finish"),
}
OPENING_KEYS = ("id", "kind", "at", "width", "sill_z", "head_z", "swing", "unit", "casing", "name", "note", "src")
UNIT_KEYS = ("frame", "verticals", "mullion", "horizontals", "rail", "glazed", "finish", "leaf_finish", "depth",
             "offset")
CASING_KEYS = ("width", "proud", "finish", "faces")
DOC_KEYS = ("ir_version", "meta", "zones", "datums", "location", "elements")
DOCUMENT = "(document)"         # the element_id of a finding about the document as a whole

_REQUIRED = object()


# ------------------------------------------------------------------ results

@dataclass
class Finding:
    element_id: str
    message: str

    def to_json(self) -> dict:
        return {"element_id": self.element_id, "message": self.message}

    def __str__(self) -> str:
        return f"{self.element_id}: {self.message}"


@dataclass
class Plan:
    elements: list = field(default_factory=list)
    refusals: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    location: dict | None = None
    zones: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refusals

    def message(self) -> str:
        """The refusals as one message, one element a line, or '' when the plan is ok."""
        if self.ok:
            return ""
        n = len(self.refusals)
        return f"{n} element{'' if n == 1 else 's'} refused:\n" + "\n".join(f"  {f}" for f in self.refusals)


class Refuse(Exception):
    """A rule an element breaks; plan_document turns it into that element's refusal."""

    def __init__(self, element_id, message):
        super().__init__(message)
        self.element_id = element_id
        self.message = message


def _show(v, n=80) -> str:
    s = json.dumps(v)
    return s if len(s) <= n else s[:n - 3] + "..."


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else _show(v)


class Fields:
    """Typed reads of one JSON object. A failure refuses the element `eid`, the message naming the field (after
    `where`, such as "opening living.wall.front.door unit: ")."""

    def __init__(self, obj, eid, where=""):
        self.h, self.eid, self.where = obj, eid, where
        if not isinstance(obj, dict):
            self.fail(f"expected a JSON object, got {_show(obj)}")

    def fail(self, msg):
        raise Refuse(self.eid, self.where + msg)

    def has(self, k) -> bool:
        return self.h.get(k) is not None

    def _get(self, k, default):
        v = self.h.get(k)
        if v is None and default is _REQUIRED:
            self.fail(f"'{k}' is missing")
        return v

    def num(self, k, default=_REQUIRED):
        v = self._get(k, default)
        if v is None:
            return default
        if not geom.is_number(v):
            self.fail(f"'{k}' must be a number, got {_show(v)}")
        return float(v)

    def pos(self, k, default=_REQUIRED):
        """A size: more than EPS, since SketchUp merges points closer than that and could not draw it."""
        v = self.num(k, default)
        if v is not None and v <= 0:
            self.fail(f"'{k}' must be greater than 0, got {_num(v)}")
        if v is not None and v <= EPS:
            self.fail(f"'{k}' is {_num(v)} in; a size must be more than {EPS:g} in, as SketchUp merges points closer "
                      "than that")
        return v

    def int(self, k, default=_REQUIRED):
        v = self._get(k, default)
        if v is None:
            return default
        if not (geom.is_number(v) and float(v) == int(v)):
            self.fail(f"'{k}' must be a whole number, got {_show(v)}")
        return int(v)

    def str(self, k, allowed=None, default=_REQUIRED):
        v = self._get(k, default)
        if v is None:
            return default
        if not isinstance(v, str):
            self.fail(f"'{k}' must be a string, got {_show(v)}")
        if allowed and v not in allowed:
            self.fail(f"'{k}' is {_show(v)}; expected one of: {' '.join(allowed)}")
        return v

    def bool(self, k, default=_REQUIRED):
        v = self._get(k, default)
        if v is None:
            return default
        if not isinstance(v, bool):
            self.fail(f"'{k}' must be true or false, got {_show(v)}")
        return v

    def list(self, k, default=_REQUIRED):
        v = self._get(k, default)
        if v is None:
            return default
        if not isinstance(v, list):
            self.fail(f"'{k}' must be a list, got {_show(v)}")
        return v

    def obj(self, k, default=_REQUIRED):
        v = self._get(k, default)
        if v is None:
            return default
        if not isinstance(v, dict):
            self.fail(f"'{k}' must be an object, got {_show(v)}")
        return v

    def vec(self, v, what, n=None):
        if not (isinstance(v, list) and all(geom.is_number(x) for x in v)):
            self.fail(f"'{what}' must be a list of numbers, got {_show(v)}")
        if n is not None and len(v) != n:
            self.fail(f"'{what}' must hold {n} numbers, got {len(v)}")
        return [float(x) for x in v]

    def point(self, k, n, default=_REQUIRED):
        v = self._get(k, default)
        return default if v is None else self.vec(v, k, n)

    def nums(self, k, default=_REQUIRED):
        v = self._get(k, default)
        return default if v is None else self.vec(v, k)

    def points(self, k, dim, minimum):
        v = self.list(k)
        if len(v) < minimum:
            self.fail(f"'{k}' needs at least {minimum} points, got {len(v)}")
        return [self.vec(p, f"{k}[{i}]", dim) for i, p in enumerate(v)]


class Context:
    """What planning one document shares: its zones, the palette's keys, the ids seen, and the warnings."""

    def __init__(self, plan, palette, zones, first):
        self.plan, self.palette, self.zones, self.first = plan, palette, zones, first

    def warn(self, eid, msg):
        self.plan.warnings.append(Finding(eid, msg))

    def finish(self, f, key, default, eid):
        """A palette key: the one `key` names, or `default`; an unknown key warns and draws as placeholder."""
        name = f.str(key, default=default)
        if name in self.palette:
            return name
        self.warn(eid, f"unknown finish {_show(name)} for '{key}' (not in build/palette.json); drawn as placeholder")
        return "placeholder"

    def unknown(self, obj, known, eid, what):
        extra = [k for k in obj if k not in known]
        if extra:
            self.warn(eid, f"{what}unknown field{'' if len(extra) == 1 else 's'} {', '.join(extra)} "
                           f"(ignored; docs/IR.md lists the fields)")


# ------------------------------------------------------------------ the palette

_palette_cache: dict = {}


def load_palette(path=PALETTE_PATH) -> dict:
    """build/palette.json: {key: {"rgb": [r, g, b], "alpha"?: a, "note"?: words}}, read once per file version."""
    path = Path(path)
    stamp = (str(path), path.stat().st_mtime_ns)
    if stamp not in _palette_cache:
        _palette_cache.clear()
        _palette_cache[stamp] = json.loads(path.read_text())
    return _palette_cache[stamp]


# ------------------------------------------------------------------ the document

def plan_document(doc, path="the document", palette=None) -> Plan:
    """Plan a house document (docs/IR.md). `path` names it in document-level messages; `palette` is a collection of
    finish keys (build/palette.json's when None)."""
    plan = Plan()
    keys = set(load_palette() if palette is None else palette)

    def refuse(msg):
        plan.refusals.append(Finding(DOCUMENT, f"{path}: {msg}"))
        return plan

    if not isinstance(doc, dict):
        return refuse("the document must be a JSON object")
    if doc.get("ir_version") != IR_VERSION or isinstance(doc.get("ir_version"), bool):
        return refuse(f"ir_version is {_show(doc.get('ir_version'))}; this planner reads ir_version {IR_VERSION}")
    els = doc.get("elements")
    if not isinstance(els, list):
        return refuse("'elements' must be a list")
    ctx = Context(plan, keys, {}, _first_uses(els))
    ctx.unknown(doc, DOC_KEYS, DOCUMENT, "the document has ")
    try:
        ctx.zones = plan.zones = _plan_zones(doc, ctx)
        plan.location = _plan_location(doc.get("location"), ctx)
        datums = doc.get("datums", {})
        if not (isinstance(datums, dict) and all(geom.is_number(v) for v in datums.values())):
            raise Refuse(DOCUMENT, f"'datums' must map names to numbers, got {_show(datums)}")
    except Refuse as e:
        return refuse(e.message)
    for i, raw in enumerate(els):
        eid = raw.get("id") if isinstance(raw, dict) and isinstance(raw.get("id"), str) else f"elements[{i}]"
        try:
            plan.elements.append(_plan_element(raw, i, eid, ctx))
        except Refuse as e:
            plan.refusals.append(Finding(e.element_id or eid, e.message))
    if plan.ok:
        _check_sills(plan.elements, ctx)
    return plan


def _first_uses(els):
    """{id: where it is first used}, over elements and their openings together, in document order."""
    first = {}
    for i, raw in enumerate(els):
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get("id"), str):
            first.setdefault(raw["id"], f"elements[{i}]")
        ops = raw.get("openings")
        for o in ops if isinstance(ops, list) else []:
            if isinstance(o, dict) and isinstance(o.get("id"), str):
                first.setdefault(o["id"], f"an opening of {raw.get('id')}")
    return first


def _plan_zones(doc, ctx):
    zones = doc.get("zones")
    if not isinstance(zones, dict) or not zones:
        raise Refuse(DOCUMENT, "'zones' must declare every zone the elements use, as {name: {\"storey\": n}}")
    out = {}
    for name, z in zones.items():
        if not ZONE_RE.fullmatch(name):
            raise Refuse(DOCUMENT, f"zone name {_show(name)} must be lowercase letters, digits and _")
        f = Fields(z, DOCUMENT, f"zone {name}: ")
        out[name] = {"storey": f.num("storey"), "outdoor": f.bool("outdoor", False)}
        ctx.unknown(z, ("storey", "outdoor", "name"), DOCUMENT, f"zone {name} has ")
    return out


def _plan_location(raw, ctx):
    if raw is None:
        return None
    f = Fields(raw, DOCUMENT, "location: ")
    ctx.unknown(raw, LOCATION_KEYS + ("note",), DOCUMENT, "location has ")
    out = {}
    for k in ("city", "country"):
        if f.has(k):
            out[k] = f.str(k)
    for k, lo, hi in (("latitude", -90.0, 90.0), ("longitude", -180.0, 180.0), ("tz_offset", -12.0, 14.0)):
        if f.has(k):
            out[k] = f.num(k)
            if not lo <= out[k] <= hi:
                f.fail(f"'{k}' must lie in {lo:g}..{hi:g}, got {out[k]:g}")
    if f.has("north_angle"):
        out["north_angle"] = f.num("north_angle")
        if not 0.0 <= out["north_angle"] < 360.0:
            f.fail(f"'north_angle' must be at least 0 and less than 360, got {out['north_angle']:g}")
    return out


def _check_id(f, eid, ctx, where):
    """Refuse an id that breaks the id rule or that something earlier in the document uses."""
    if not ID_RE.fullmatch(eid):
        f.fail(f"id {_show(eid)} must be lowercase, dotted, with at least two tokens (like kitchen.wall.north)")
    if ctx.first.get(eid) != where:
        f.fail(f"duplicate id {_show(eid)}: {ctx.first.get(eid)} uses it first")


def _plan_element(raw, i, eid, ctx):
    f = Fields(raw, eid)
    if not isinstance(raw.get("id"), str):
        f.fail("an element needs a string 'id'")
    _check_id(f, eid, ctx, f"elements[{i}]")
    t = f.str("type", TYPES)
    zone = f.str("zone")
    if zone not in ctx.zones:
        f.fail(f"zone {_show(zone)} is not declared in the document's 'zones' ({', '.join(ctx.zones)})")
    out = {"id": eid, "type": t, "zone": zone, "src": f.str("src", SRCS)}
    for k in ("name", "note"):
        if f.has(k):
            out[k] = f.str(k)
    if f.has("tags"):
        tags = f.list("tags")
        if not all(isinstance(x, str) for x in tags):
            f.fail(f"'tags' must be a list of strings, got {_show(tags)}")
        out["tags"] = tags
    PLANNERS[t](f, out, ctx)
    ctx.unknown(raw, COMMON + KNOWN[t], eid, "")
    return out


def _ring(f, key):
    pts = f.points(key, 2, 3)
    try:
        ring = geom.clean_ring(pts, 2)
    except ValueError as e:
        f.fail(f"'{key}' {e}")
    if abs(geom.area2(ring)) < 0.01:
        f.fail(f"'{key}' encloses no area")
    touch = geom.self_touch(ring)
    if touch:
        f.fail(f"'{key}' is not a simple polygon: edge {touch[0]} touches edge {touch[1]}")
    return ring


# ------------------------------------------------------------------ types

def _plan_space(f, out, ctx):
    eid = out["id"]
    out["outline"] = _ring(f, "outline")
    out["floor_z"] = f.num("floor_z")
    cz = f.num("ceiling_z", None)
    plane = None
    if f.has("ceiling_plane"):
        raw = f.obj("ceiling_plane")
        pf = Fields(raw, eid, "ceiling_plane: ")
        plane = {"at": pf.point("at", 3), "slope": pf.point("slope", 2)}
        ctx.unknown(raw, ("at", "slope"), eid, "ceiling_plane has ")
    if cz is not None and plane is not None:
        f.fail("give one of ceiling_z and ceiling_plane, not both")
    out["ceiling_z"], out["ceiling_plane"] = cz, plane
    has_ceiling = cz is not None or plane is not None
    out["build_floor"] = f.bool("build_floor", True)
    out["build_ceiling"] = f.bool("build_ceiling", has_ceiling)
    if out["build_ceiling"] and not has_ceiling:
        f.fail("build_ceiling is true but the space has neither ceiling_z nor ceiling_plane")
    if has_ceiling:
        for k, (x, y) in enumerate(out["outline"]):
            z = geom.ceiling_at(out, x, y)
            if z <= out["floor_z"] + EPS:
                f.fail(f"the ceiling stands at z {z:g} over outline corner {k} ({x:g}, {y:g}), not above floor_z "
                       f"{out['floor_z']:g}")
    out["floor_finish"] = ctx.finish(f, "floor_finish", DEFAULT_FINISH["floor"], eid)
    out["ceiling_finish"] = ctx.finish(f, "ceiling_finish", DEFAULT_FINISH["ceiling"], eid)


def _plan_slab(f, out, ctx):
    out["outline"] = _ring(f, "outline")
    out["z_top"] = f.num("z_top")
    out["thickness"] = f.pos("thickness")
    out["finish"] = ctx.finish(f, "finish", DEFAULT_FINISH["slab"], out["id"])


def _segment(f, out):
    out["a"], out["b"] = f.point("a", 2), f.point("b", 2)
    out["length"] = math.hypot(out["b"][0] - out["a"][0], out["b"][1] - out["a"][1])
    if out["length"] < 0.01:
        f.fail(f"a and b coincide (length {out['length']:g})")


def _plan_wall(f, out, ctx):
    eid = out["id"]
    _segment(f, out)
    out["thickness"] = f.pos("thickness")
    out["z0"] = f.num("z0")
    out["top_a"], out["top_b"] = f.num("top_a"), f.num("top_b")
    for k in ("top_a", "top_b"):
        if not out[k] > out["z0"] + EPS:
            f.fail(f"{k} {out[k]:g} is not above z0 {out['z0']:g}")
    out["kind"] = f.str("kind", WALL_KINDS)
    out["finish"] = ctx.finish(f, "finish", DEFAULT_FINISH["wall"][out["kind"]], eid)
    out["finish_left"] = ctx.finish(f, "finish_left", out["finish"], eid)
    out["finish_right"] = ctx.finish(f, "finish_right", out["finish"], eid)
    raws = f.list("openings", [])
    ops = [_plan_opening(out, raw, i, ctx) for i, raw in enumerate(raws)]
    for p, q in zip(ops, ops[1:]):
        if q["rect"][0] < p["rect"][1] - EPS:
            f.fail(f"openings {p['id']} ({p['rect'][0]:g}..{p['rect'][1]:g}) and {q['id']} "
                   f"({q['rect'][0]:g}..{q['rect'][1]:g}) are out of order along the wall or overlap")
        if q["rect"][0] < p["rect"][1] + EPS:
            q["rect"][0] = p["rect"][1]          # two openings that meet share one jamb line
    for o, raw in zip(ops, raws):
        _plan_unit_and_casing(out, o, raw, ctx)
    _casings_apart(f, ops)
    out["openings"] = ops
    _slivers(out, ctx)


def _plan_opening(wall, raw, i, ctx):
    eid = wall["id"]
    oid = raw.get("id") if isinstance(raw, dict) else None
    f = Fields(raw, eid, f"opening {oid or f'openings[{i}]'}: ")
    if not isinstance(oid, str):
        f.fail("an opening needs a string 'id'")
    _check_id(f, oid, ctx, f"an opening of {eid}")
    kind = f.str("kind", OPENING_KINDS)
    at, width = f.num("at"), f.pos("width")
    sill, head = f.num("sill_z"), f.num("head_z")
    s, e, length = at, at + width, wall["length"]
    if s < -EPS:
        f.fail(f"at {at:g} lies before the start of the wall")
    if e > length + EPS:
        f.fail(f"at + width = {e:g} runs past the end of the {length:g} in wall")
    s, e = (0.0 if s < EPS else s), (length if e > length - EPS else e)
    if not sill < head - EPS:
        f.fail(f"sill_z {sill:g} is not below head_z {head:g}")
    if sill < wall["z0"] - EPS:
        f.fail(f"sill_z {sill:g} lies below the wall's base z0 {wall['z0']:g}")
    if sill < wall["z0"] + EPS:
        sill = wall["z0"]
    tmin, tmax = geom.wall_top_range(wall, s, e)
    if head > tmin + EPS:
        f.fail(f"head_z {head:g} rises above the wall's top ({tmin:g}) over the opening")
    if head > tmin - EPS and tmax - tmin < EPS:
        head = tmin
    o = {"id": oid, "kind": kind, "at": at, "width": width, "sill_z": f.num("sill_z"), "head_z": f.num("head_z"),
         "rect": [s, e, sill, head], "swing": None, "unit": None, "casing": None}
    for k in ("name", "note"):
        if f.has(k):
            o[k] = f.str(k)
    if f.has("src"):
        o["src"] = f.str("src", SRCS)
    if f.has("swing"):
        if kind != "door":
            f.fail(f"a {kind} opening takes no swing (doors only)")
        sw = f.obj("swing")
        sf = Fields(sw, eid, f"opening {oid} swing: ")
        o["swing"] = {"hinge": sf.str("hinge", HINGES), "side": sf.str("side", ("left", "right"))}
        ctx.unknown(sw, ("hinge", "side"), eid, f"opening {oid} swing has ")
    ctx.unknown(raw, OPENING_KEYS, eid, f"opening {oid} has ")
    return o


def _plan_unit_and_casing(wall, o, raw, ctx):
    f = Fields(raw, wall["id"], f"opening {o['id']}: ")
    if f.has("unit"):
        if o["kind"] == "pass":
            f.fail("a pass opening takes no unit (it is a plain hole)")
        o["unit"] = _plan_unit(wall, o, f.obj("unit"), ctx)
    if f.has("casing"):
        if o["kind"] == "pass":
            f.fail("a pass opening takes no casing")
        o["casing"] = _plan_casing(wall, o, f.obj("casing"), ctx)
    elif o["kind"] == "cased":
        o["casing"] = _default_casing(wall, o, ctx)


def _frame_sides(f, kind):
    raw = f.h.get("frame")
    if not isinstance(raw, dict):
        if not (raw is None or geom.is_number(raw)):
            f.fail(f"'frame' must be a number or an object {{left, right, bottom, top}}, got {_show(raw)}")
        w = 0.0 if raw is None else float(raw)
        if w < 0 or 0 < w <= EPS:
            f.fail(f"'frame' must be 0 (no frame) or more than {EPS:g} in, got {w:g}")
        # a number frames a window on four sides, a door or cased opening on three: no member across the threshold
        return {"left": w, "right": w, "bottom": w if kind == "window" else 0.0, "top": w}
    ff = Fields(raw, f.eid, f.where + "frame: ")
    sides = {}
    for k in SIDES:
        sides[k] = ff.num(k)
        if sides[k] < 0 or 0 < sides[k] <= EPS:
            ff.fail(f"'{k}' must be 0 (no member on that side) or more than {EPS:g} in, got {sides[k]:g}")
    return sides


def _spans(f, lo, hi, centres, w, what, shown_from):
    """[lo, hi] split by bars `w` wide centred on each of `centres`; every bar leaves a light on both sides."""
    out, cur = [], lo
    for c in centres:
        a = c - w / 2.0
        if a - cur <= EPS:
            f.fail(f"the {what} at {c - shown_from:g} leaves no light before it (bar {a:g}..{a + w:g}, the light "
                   f"starts at {cur:g})")
        out.append([cur, a])
        cur = c + w / 2.0
    if hi - cur <= EPS:
        f.fail(f"the last {what} leaves no light before the frame ({cur:g} against {hi:g})")
    out.append([cur, hi])
    return out


def _plan_unit(wall, o, raw, ctx):
    eid, oid, kind = wall["id"], o["id"], o["kind"]
    f = Fields(raw, eid, f"opening {oid} unit: ")
    sides = _frame_sides(f, kind)
    verticals, horizontals = f.nums("verticals", []), f.nums("horizontals", [])
    mullion = f.pos("mullion") if verticals else f.num("mullion", 0.0)
    rail = f.pos("rail") if horizontals else f.num("rail", 0.0)
    glazed = f.bool("glazed", kind == "window")
    if f.has("leaf_finish") and (glazed or kind == "cased"):
        ctx.warn(oid, "leaf_finish paints nothing: " + ("the unit is glazed" if glazed else "a cased opening has no leaf"))
    t = wall["thickness"]
    depth = f.pos("depth") if f.has("depth") else min(t, UNIT_DEPTH_MAX)
    if depth > t + EPS:
        f.fail(f"depth {depth:g} is deeper than the {t:g} in wall")
    depth = min(depth, t)
    room = (t - depth) / 2.0
    offset = f.num("offset", 0.0)
    if abs(offset) > room + EPS:
        f.fail(f"offset {offset:g} takes the {depth:g} in frame out of the {t:g} in wall (it has {room:g} in either "
               "way of the centreline)")
    offset = max(-room, min(room, offset)) + 0.0         # + 0.0 turns -0.0 into 0.0
    if any(q <= p for p, q in zip(verticals, verticals[1:])):
        f.fail("verticals must increase")
    if any(q <= p for p, q in zip(horizontals, horizontals[1:])):
        f.fail("horizontals must increase")
    s, e, sill, head = o["rect"]
    d_lo, d_hi = s + sides["left"], e - sides["right"]
    z_lo, z_hi = sill + sides["bottom"], head - sides["top"]
    if d_hi - d_lo <= EPS or z_hi - z_lo <= EPS:
        f.fail(f"the frame ({', '.join(f'{k} {sides[k]:g}' for k in SIDES)}) leaves no light in the "
               f"{e - s:g} x {head - sill:g} opening")
    cols = _spans(f, d_lo, d_hi, [s + v for v in verticals], mullion, "mullion", s)
    rows = _spans(f, z_lo, z_hi, horizontals, rail, "rail at z", 0.0)
    unit = {"frame": sides, "verticals": verticals, "mullion": mullion, "horizontals": horizontals, "rail": rail,
            "glazed": glazed, "finish": ctx.finish(f, "finish", DEFAULT_FINISH["unit"], oid),
            "leaf_finish": ctx.finish(f, "leaf_finish", DEFAULT_FINISH["leaf"], oid),
            "depth": depth, "offset": offset, "lights": [[d0, d1, z0, z1] for z0, z1 in rows for d0, d1 in cols]}
    ctx.unknown(raw, UNIT_KEYS, oid, "unit has ")
    return unit


def _casing_fits(f, wall, o, sides, what):
    s, e, sill, head = o["rect"]
    d0, d1, z0, z1 = s - sides["left"], e + sides["right"], sill - sides["bottom"], head + sides["top"]
    if d0 < -EPS:
        f.fail(f"{what} runs {-d0:g} in past end a of the wall")
    if d1 > wall["length"] + EPS:
        f.fail(f"{what} runs {d1 - wall['length']:g} in past end b of the {wall['length']:g} in wall")
    if z0 < wall["z0"] - EPS:
        f.fail(f"{what}'s foot z {z0:g} lies below the wall's base z0 {wall['z0']:g}")
    top = geom.wall_top_range(wall, max(d0, 0.0), min(d1, wall["length"]))[0]
    if z1 > top + EPS:
        f.fail(f"{what}'s head z {z1:g} rises above the wall's top ({top:g}) over it")
    return [max(d0, 0.0), min(d1, wall["length"]), max(z0, wall["z0"]), min(z1, top)]


def _plan_casing(wall, o, raw, ctx):
    eid, oid = wall["id"], o["id"]
    f = Fields(raw, eid, f"opening {oid} casing: ")
    w = raw.get("width")
    if isinstance(w, dict):
        wf = Fields(w, eid, f.where + "width: ")
        sides = {}
        for k in SIDES:
            sides[k] = wf.num(k)
            if not (sides[k] == 0 or sides[k] > EPS):
                wf.fail(f"'{k}' must be 0 (no board on that side) or more than {EPS:g} in, got {sides[k]:g}")
        if all(v == 0 for v in sides.values()):
            wf.fail("every side is 0, so there is no casing")
    else:
        n = f.pos("width", CASING_WIDTH)
        sides = {"left": n, "right": n, "bottom": n if o["kind"] == "window" else 0.0, "top": n}
    proud = f.pos("proud", CASING_PROUD)
    faces = f.str("faces", FACES, "both")
    ctx.unknown(raw, CASING_KEYS, oid, "casing has ")
    return {"sides": sides, "proud": proud, "finish": ctx.finish(f, "finish", DEFAULT_FINISH["casing"], oid),
            "faces": ["left", "right"] if faces == "both" else [faces],
            "outer": _casing_fits(f, wall, o, sides, "the casing")}


def _default_casing(wall, o, ctx):
    """A cased opening that names no casing takes CASING_WIDTH boards on both faces and three sides, where the wall has
    room for them; otherwise it is built without trim, with a warning."""
    w = CASING_WIDTH
    sides = {"left": w, "right": w, "bottom": 0.0, "top": w}
    try:
        outer = _casing_fits(Fields({}, wall["id"]), wall, o, sides, "the casing")
    except Refuse:
        ctx.warn(o["id"], f"no room in the wall for the default {w:g} in casing round this cased opening; built "
                          "without trim (name a 'casing' to choose its sides)")
        return None
    return {"sides": sides, "proud": CASING_PROUD, "finish": DEFAULT_FINISH["casing"], "faces": ["left", "right"],
            "outer": outer}


def _casings_apart(f, ops):
    cased = [o for o in ops if o["casing"]]
    for i, p in enumerate(cased):
        for q in cased[i + 1:]:
            shared = [x for x in p["casing"]["faces"] if x in q["casing"]["faces"]]
            (pd0, pd1, pz0, pz1), (qd0, qd1, qz0, qz1) = p["casing"]["outer"], q["casing"]["outer"]
            if shared and min(pd1, qd1) - max(pd0, qd0) > EPS and min(pz1, qz1) - max(pz0, qz0) > EPS:
                f.fail(f"the casings of {p['id']} and {q['id']} overlap on the wall's {' and '.join(shared)} "
                       f"face{'' if len(shared) == 1 else 's'}; narrow a side (a casing width object) or give one "
                       "'faces'")


def _slivers(wall, ctx):
    """Warn of piers, sills and lintels so thin SketchUp may merge them away (not refusals: they often draw)."""
    ops = wall["openings"]
    for o in ops:
        s, e, sill, head = o["rect"]
        top = geom.wall_top_range(wall, s, e)[0]
        for gap, what in ((s, "pier at end a"), (wall["length"] - e, "pier at end b"),
                          (sill - wall["z0"], "sill under it"), (top - head, "lintel over it")):
            if 0 < gap < SLIVER:
                ctx.warn(wall["id"], f"{o['id']} leaves a {gap:.4f} in {what}; SketchUp may not draw a sliver")
    for p, q in zip(ops, ops[1:]):
        gap = q["rect"][0] - p["rect"][1]
        if EPS <= gap < SLIVER:
            ctx.warn(wall["id"], f"{p['id']} and {q['id']} leave a {gap:.4f} in pier between them")


def _plan_column(f, out, ctx):
    out["center"] = f.point("center", 2)
    out["shape"] = f.str("shape", SHAPES)
    out["size"] = out["diameter"] = None
    if out["shape"] == "round":
        if f.has("size"):
            f.fail("a round column takes 'diameter', not 'size'")
        out["diameter"] = f.pos("diameter")
        if out["diameter"] * math.sin(math.pi / ROUND_SIDES) <= EPS:
            f.fail(f"a round column of diameter {out['diameter']:g} draws {ROUND_SIDES} sides each {EPS:g} in or less, "
                   "which SketchUp merges")
    else:
        if f.has("diameter"):
            f.fail("a square column takes 'size' [w, d], not 'diameter'")
        out["size"] = f.point("size", 2)
        if not all(v > EPS for v in out["size"]):
            f.fail(f"'size' must be more than {EPS:g} in each way, got {_show(out['size'])}")
    out["z0"], out["z1"] = f.num("z0"), f.num("z1")
    if not out["z1"] > out["z0"] + EPS:
        f.fail(f"z1 {out['z1']:g} is not above z0 {out['z0']:g}")
    out["capital"] = None
    if f.has("capital"):
        raw = f.obj("capital")
        cf = Fields(raw, out["id"], "capital: ")
        size = cf.point("size", 2)
        if not all(v > EPS for v in size):
            cf.fail(f"'size' must be more than {EPS:g} in each way, got {_show(size)}")
        height = cf.pos("height")
        if height >= out["z1"] - out["z0"] - EPS:
            cf.fail(f"height {height:g} leaves no shaft in the {out['z1'] - out['z0']:g} in column")
        out["capital"] = {"size": size, "height": height}
        ctx.unknown(raw, ("size", "height"), out["id"], "capital has ")
    out["finish"] = ctx.finish(f, "finish", DEFAULT_FINISH["column"], out["id"])


def _plan_beam(f, out, ctx):
    _segment(f, out)
    out["width"], out["depth"] = f.pos("width"), f.pos("depth")
    out["soffit_z"] = f.num("soffit_z")
    out["kind"] = f.str("kind", BEAM_KINDS)
    out["finish"] = ctx.finish(f, "finish", DEFAULT_FINISH["beam"][out["kind"]], out["id"])


def _plan_stair(f, out, ctx):
    _segment(f, out)
    out["descend"] = dx, dy = f.point("descend", 2)
    if abs(math.hypot(dx, dy) - 1.0) > 1e-6:
        f.fail(f"descend {_show(out['descend'])} is not a unit vector")
    ux, uy = (out["b"][0] - out["a"][0]) / out["length"], (out["b"][1] - out["a"][1]) / out["length"]
    if abs(dx * ux + dy * uy) > 1e-6:
        f.fail(f"descend {_show(out['descend'])} is not square to a -> b")
    out["z_top"] = f.num("z_top")
    out["risers"] = f.int("risers")
    if out["risers"] < 1:
        f.fail(f"risers must be at least 1, got {out['risers']}")
    out["rise"], out["run"] = f.pos("rise"), f.pos("run")
    out["finish"] = ctx.finish(f, "finish", DEFAULT_FINISH["stair"], out["id"])


def _plan_box(f, out, ctx):
    out["bbox"] = x0, y0, z0, x1, y1, z1 = f.point("bbox", 6)
    if not (x1 - x0 > EPS and y1 - y0 > EPS and z1 - z0 > EPS):
        f.fail(f"bbox [x0, y0, z0, x1, y1, z1] needs each minimum below its maximum, got {_show(out['bbox'])}")
    out["kind"] = f.str("kind", BOX_KINDS)
    out["subtype"] = f.str("subtype", None, None)
    out["front"] = f.str("front", FRONTS, None)
    out["seams"], out["rails"] = f.nums("seams", []), f.nums("rails", [])
    if out["front"] is None and (out["seams"] or out["rails"]):
        f.fail("seams and rails are drawn on the front face, but the box names no 'front'")
    if out["front"] is not None:
        lo, hi = (x0, x1) if out["front"][1] == "y" else (y0, y1)
        for k in ("seams", "rails"):
            if any(q - p <= EPS for p, q in zip(out[k], out[k][1:])):
                f.fail(f"{k} must increase")
        for s in out["seams"]:
            if not EPS < s < hi - lo - EPS:
                f.fail(f"seam {s:g} does not lie inside the {hi - lo:g} in front (seams are measured from its low end)")
        for r in out["rails"]:
            if not z0 + EPS < r < z1 - EPS:
                f.fail(f"rail z {r:g} does not lie inside z {z0:g}..{z1:g} (rails are absolute z)")
    out["finish"] = ctx.finish(f, "finish", DEFAULT_FINISH["box"][out["kind"]], out["id"])


def _plan_roof_plane(f, out, ctx):
    try:
        pts = geom.clean_ring(f.points("pts", 3, 3), 3)
    except ValueError as e:
        f.fail(f"'pts' {e}")
    nx, ny, nz = geom.newell(pts)
    n = math.sqrt(nx * nx + ny * ny + nz * nz)
    if n < 1e-9:
        f.fail("'pts' describe a degenerate polygon")
    nx, ny, nz = nx / n, ny / n, nz / n
    c = [sum(p[k] for p in pts) / len(pts) for k in range(3)]
    off = max(abs((p[0] - c[0]) * nx + (p[1] - c[1]) * ny + (p[2] - c[2]) * nz) for p in pts)
    if off > 0.01:
        f.fail(f"'pts' are not planar: a point lies {off:.4f} in off their best plane")
    if abs(nz) < 0.02:
        f.fail("the roof plane is vertical, so it cannot hang a thickness below its top")
    plan2 = [p[:2] for p in pts]
    touch = geom.self_touch(plan2)
    if touch:
        f.fail(f"'pts' seen from above are not a simple polygon: edge {touch[0]} touches edge {touch[1]}")
    if off > 1e-4:
        ctx.warn(out["id"], f"pts lie up to {off:.5f} in off one plane; SketchUp may refuse the face")
    out["pts"] = pts
    out["thickness"] = f.pos("thickness")
    holes = []
    for k, raw in enumerate(f.list("holes", [])):
        x0, y0, x1, y1 = f.vec(raw, f"holes[{k}]", 4)
        if not (x1 - x0 > EPS and y1 - y0 > EPS):
            f.fail(f"holes[{k}] is [x0, y0, x1, y1] with each minimum below its maximum, got {_show(raw)}")
        ring = geom.rect_corners([x0, y0, x1, y1])
        centre = [(x0 + x1) / 2.0, (y0 + y1) / 2.0]
        clear = all(geom.segment_polygon_distance(plan2[j], plan2[(j + 1) % len(plan2)], ring) > EPS
                    for j in range(len(plan2)))
        if not (geom.inside_polygon(plan2, centre) and clear):
            f.fail(f"holes[{k}] does not lie inside the plane's outline, more than {EPS:g} in from its edges")
        for j, other in enumerate(holes):
            if geom.polygon_distance(ring, geom.rect_corners(other)) <= EPS:
                f.fail(f"holes[{j}] and holes[{k}] overlap or touch")
        holes.append([x0, y0, x1, y1])
    out["holes"] = holes
    out["finish"] = ctx.finish(f, "finish", DEFAULT_FINISH["roof_plane"], out["id"])


PLANNERS = {"space": _plan_space, "slab": _plan_slab, "wall": _plan_wall, "column": _plan_column, "beam": _plan_beam,
            "stair": _plan_stair, "box": _plan_box, "roof_plane": _plan_roof_plane}


# ------------------------------------------------------------------ across elements

def _check_sills(elements, ctx):
    """Warn of a door or cased opening whose sill stands on no floor: neither within SILL_TOL of its wall's base nor of
    the floor of a space just past either face of the wall at its middle. Usually a z typed against the wrong datum."""
    spaces = [e for e in elements if e["type"] == "space"]
    for wall in (e for e in elements if e["type"] == "wall"):
        for o in wall["openings"]:
            if o["kind"] not in ("door", "cased"):
                continue
            s, e, sill, _ = o["rect"]
            if abs(sill - wall["z0"]) <= SILL_TOL:
                continue
            seen = []
            for side in (1.0, -1.0):
                for d in SILL_PROBES:
                    p = geom.wall_point(wall, (s + e) / 2.0, 0.0, side * (wall["thickness"] / 2.0 + d))
                    seen += [sp for sp in geom.space_at(spaces, p[0], p[1]) if sp not in seen]
            if any(abs(sp["floor_z"] - sill) <= SILL_TOL for sp in seen):
                continue
            floors = ", ".join(f"{sp['id']} at {sp['floor_z']:g}" for sp in seen) or "no space found beside it"
            ctx.warn(o["id"], f"the sill at z {sill:g} stands on no floor beside the wall ({floors}) nor on the "
                              f"wall's base z0 {wall['z0']:g}")


# ------------------------------------------------------------------ the plan file

def write_plan(plan: Plan, ir_path, ir_sha256: str, out_path) -> Path:
    """Write the plan build.rb reads: {plan, ir, ir_sha256, elements, location, zones, warnings}. The file is left as
    it is when it already holds this plan, so its time stamp says when the plan last changed. Raises ValueError for a
    plan with refusals: there is nothing for build.rb to draw."""
    if not plan.ok:
        raise ValueError(f"refusing to write a plan of {ir_path}: {plan.message()}")
    body = {"plan": PLAN_VERSION, "ir": str(Path(ir_path).resolve()), "ir_sha256": ir_sha256,
            "elements": plan.elements, "location": plan.location, "zones": plan.zones,
            "warnings": [w.to_json() for w in plan.warnings]}
    text = json.dumps(body, indent=None, separators=(",", ":"), allow_nan=False) + "\n"
    out = Path(out_path)
    try:
        if out.read_text() == text:
            return out
    except OSError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}")
    tmp.write_text(text)
    tmp.replace(out)
    return out


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ir.plan", description=__doc__.splitlines()[0])
    ap.add_argument("ir", help="a house document, such as house.json")
    ap.add_argument("-o", "--out", help="also write the plan file here (only when nothing is refused)")
    ap.add_argument("--json", action="store_true", help="print the findings as JSON")
    a = ap.parse_args(argv)
    path = Path(a.ir)
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"{path}: not valid JSON: {e}", file=sys.stderr)
        return 1
    plan = plan_document(doc, str(path))
    if a.json:
        print(json.dumps({"ok": plan.ok, "elements": len(plan.elements),
                          "refusals": [f.to_json() for f in plan.refusals],
                          "warnings": [f.to_json() for f in plan.warnings]}, indent=1))
    else:
        for f in plan.refusals:
            print(f"REFUSED {f}")
        for f in plan.warnings:
            print(f"warning {f}")
        print(f"{path}: {len(plan.elements)} elements planned, {len(plan.refusals)} refused, "
              f"{len(plan.warnings)} warning{'' if len(plan.warnings) == 1 else 's'}")
    if a.out and plan.ok:
        write_plan(plan, path, sha256_bytes(raw), a.out)
    return 0 if plan.ok else 1


if __name__ == "__main__":
    sys.exit(main())
