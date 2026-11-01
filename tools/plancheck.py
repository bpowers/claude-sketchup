"""The compiled house drawn over an architect's sheet, or on its own, to check it against the drawing.

A plan cut is drawn over a plan sheet, an elevation over an elevation sheet.

    uv run python -m tools.plancheck NAME [--z 48] [--dpi 150] [--window X0 Y0 X1 Y1] [--layers L ...] [--ids]
    uv run python -m tools.plancheck NAME --elevation -y [--depth 36] [--window U0 Z0 U1 Z1] [--layers L ...] [--ids]
    uv run python -m tools.plancheck --plain [--z 48 | --elevation -y] [--px-per-ft 40] [--window ...] [--ids]

NAME is a registration in sources/plans/registrations.json (extract/pdf.py): the sheet is rendered, faded to grey,
and house.json is drawn over it through the registration, written to out/plancheck/<NAME>.png. It is the drawing's
equivalent of a photo overlay: where the model's lines and the sheet's part, one of them is wrong.

The plan (the default) is house.json's cut at the absolute height z (inches; default 48, four feet over a main floor
at z 0), from the elements standing within a storey of the cut:
- walls the cut passes through, filled red, with the openings the cut passes through taken out; an opening above or
  below the cut (a high window) outlined dashed over its wall
- windows: a cyan line across the opening (the glass); doors: green, the leaf and its swing where the door records
  one; cased openings and passes: orange jambs
- spaces: their outlines, dashed blue, named by id
- boxes: on the floor or cut, outlined orange; overhead (upper cabinets, soffits), dashed purple; beams, dashed purple
- columns filled red, stairs as their nosings in brown, slabs at or below the cut in grey
Each cut wall's two faces are matched to the nearest parallel sheet line within --search inches, the offsets printed
(positive: the sheet's line stands outside the model's face) and any over --tol written beside the face on the image.

--elevation FACING draws the facade whose outside faces FACING (-y, +y, -x or +x) as an elevation sheet draws it: the
exterior walls within --depth inches of its front (red), their openings (windows cyan, doors green, cased orange), the
roof planes' outlines with their thickness (brown), what stands in outdoor zones (a porch's slab, steps, posts and
beams), and every datum of house.json as a dashed grey line. Coordinates are (u, z): z the absolute height and u the
model coordinate along the facade, increasing left to right as the sheet draws it from outside, which is x for a
facade facing -y, -x for +y, y for +x and -y for -x. So an elevation is registered with point a at a corner of the
facade on the main floor and --a-model U 0, U that corner's u (extract.pdf calibrate; docs/DRAWINGS.md, step 8). Each
horizontal model line (a sill, a head, a wall's top, an eave) is matched to the nearest level sheet line within
--search inches, and the offsets are printed and written as for the plan's faces (positive: the sheet's line is
higher).

The lines measured against are those on the CAD layers --layers names (the ones that draw walls, or the elevation's
lines; extract.pdf layers lists a sheet's). A sheet with no CAD layers needs no --layers: it is measured against every
line on it, where a dimension or border line can match too, so the image is still the judge. On a sheet with layers
and no --layers given, nothing is measured and the note names its layers.

--plain draws the same plan or elevation alone on a one-foot grid, to scale, with no sheet: out/plancheck/plan.png or
elevation<FACING>.png. --house takes any compiled document in place of house.json, such as a design view's
out/options/<view>.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon
from shapely.ops import unary_union

from extract import pdf as drawings

ROOT = Path(__file__).resolve().parent.parent
HOUSE = ROOT / "house.json"
OUT = ROOT / "out" / "plancheck"
CUT_Z = 48.0
BELOW = 96.0               # a storey under the cut: spaces, boxes and slabs this far down belong to the plan
ABOVE = 72.0               # and over it: what hangs overhead (upper cabinets, beams)
MARGIN = 48.0              # inches of sheet shown around the house
COLOURS = {
    "wall": (220, 30, 30), "window": (0, 170, 255), "door": (0, 150, 0), "cased": (240, 140, 0),
    "space": (50, 60, 230), "box": (255, 110, 0), "over": (150, 0, 200), "column": (220, 30, 30),
    "stair": (130, 70, 10), "slab": (120, 120, 120), "misfit": (230, 0, 160), "roof": (150, 80, 20),
    "datum": (110, 110, 110),
}
# The direction each facing's facade looks, its outward normal in plan.
FACINGS = {"-y": (0.0, -1.0), "+y": (0.0, 1.0), "-x": (-1.0, 0.0), "+x": (1.0, 0.0)}
DEPTH = 36.0               # in: exterior walls this far behind a facade's front still draw (a recessed entry, a bay)
FACADE_KINDS = ("exterior", "masonry", "glass_wall")


# ------------------------------------------------------------------ the cut

def wall_frame(w):
    """(unit vector a->b, unit normal to its left, length)."""
    (ax, ay), (bx, by) = w["a"], w["b"]
    length = math.hypot(bx - ax, by - ay)
    u = ((bx - ax) / length, (by - ay) / length)
    return u, (-u[1], u[0]), length


def wall_point(w, s, t):
    """The point s along the wall from a and t across it toward its left face."""
    u, n, _ = wall_frame(w)
    return (w["a"][0] + u[0] * s + n[0] * t, w["a"][1] + u[1] * s + n[1] * t)


def wall_quad(w, s0, s1, t0=None, t1=None):
    h = w["thickness"] / 2.0
    t0, t1 = (-h if t0 is None else t0), (h if t1 is None else t1)
    return [wall_point(w, s0, t0), wall_point(w, s1, t0), wall_point(w, s1, t1), wall_point(w, s0, t1)]


def opening_cut(o, z):
    return o["sill_z"] <= z < o["head_z"]


def plan_cut(doc, z=CUT_Z):
    """The elements a plan cut at z shows, by what it draws them as (module doc)."""
    out = {k: [] for k in ("walls", "spaces", "floor", "over", "columns", "beams", "stairs", "slabs")}
    for e in doc.get("elements", []):
        t = e.get("type")
        try:
            if t == "wall":
                top = max(e.get("top_a", 0.0), e.get("top_b", 0.0))
                if e["z0"] <= z < top and math.dist(e["a"], e["b"]) > 1e-6:
                    out["walls"].append(e)
            elif t == "space":
                if z - BELOW < e["floor_z"] <= z:
                    out["spaces"].append(e)
            elif t == "box":
                x0, y0, z0, x1, y1, z1 = e["bbox"]
                if z1 > z - BELOW and z0 < z + ABOVE:
                    out["over" if z0 >= z else "floor"].append(e)
            elif t == "column":
                if e["z0"] <= z < e["z1"]:
                    out["columns"].append(e)
            elif t == "beam":
                if z < e["soffit_z"] < z + ABOVE:
                    out["beams"].append(e)
            elif t == "stair":
                bottom = e["z_top"] - e["risers"] * e["rise"]
                if bottom < z + ABOVE and e["z_top"] > z - BELOW:
                    out["stairs"].append(e)
            elif t == "slab":
                if z - BELOW < e["z_top"] <= z:
                    out["slabs"].append(e)
        except (KeyError, TypeError, ValueError):
            continue                               # the planner reports malformed elements; the plan skips them
    return out


def walls_solid(walls, z):
    """The cut walls as one shapely geometry, the openings the cut passes through taken out."""
    solids, holes = [], []
    for w in walls:
        _, _, length = wall_frame(w)
        solids.append(Polygon(wall_quad(w, 0.0, length)))
        h = w["thickness"] / 2.0 + 0.5
        for o in w.get("openings", []):
            if opening_cut(o, z):
                holes.append(Polygon(wall_quad(w, o["at"], o["at"] + o["width"], -h, h)))
    return unary_union(solids).difference(unary_union(holes)) if solids else Polygon()


def extent(cut):
    """(x0, y0, x1, y1) of everything the cut draws, in model inches; None when it draws nothing."""
    pts = []
    for w in cut["walls"]:
        pts += wall_quad(w, 0.0, wall_frame(w)[2])
    for s in cut["spaces"] + cut["slabs"]:
        pts += [tuple(p) for p in s["outline"]]
    for b in cut["floor"] + cut["over"]:
        pts += [(b["bbox"][0], b["bbox"][1]), (b["bbox"][3], b["bbox"][4])]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ------------------------------------------------------------------ the elevation

def facade_axes(facing):
    """(the plan direction u runs along, left to right seen from outside; the direction toward the viewer)."""
    fx, fy = FACINGS[facing]
    return (-fy, fx), (fx, fy)


def _dot(p, v):
    return p[0] * v[0] + p[1] * v[1]


def elevation_cut(doc, facing, depth=DEPTH):
    """What the elevation of the facade facing `facing` shows, in (u, z) (module doc): {"shapes": [(colour, points,
    closed)], "edges": [(id, what, u0, u1, z)], the level lines to measure, "labels": [(id, (u, z))], "datums":
    {name: z}}, or None when the document has no exterior wall."""
    right, toward = facade_axes(facing)
    els = doc.get("elements", [])
    walls = [e for e in els if e.get("type") == "wall" and e.get("kind") in FACADE_KINDS
             and math.dist(e["a"], e["b"]) > 1e-6]
    if not walls:
        return None
    front = max(_dot(c, toward) for w in walls for c in wall_quad(w, 0.0, wall_frame(w)[2]))
    shapes, edges, labels = [], [], []

    def uz(p, z):
        return (_dot(p, right), z)

    def box(colour, us, z0, z1, eid=None, what=None):
        u0, u1 = min(us), max(us)
        shapes.append((colour, [(u0, z0), (u1, z0), (u1, z1), (u0, z1)], True))
        if eid:
            edges.append((eid, what, u0, u1, z1))

    for w in walls:
        _, n, length = wall_frame(w)
        quad = wall_quad(w, 0.0, length)
        if max(_dot(c, toward) for c in quad) < front - depth:
            continue
        tops = (w["top_a"], w["top_b"])
        facing_n = _dot(n, toward)
        if abs(facing_n) < 1e-6:                        # seen edge on: a strip as wide as the wall is thick, as
            near = tops[0] if _dot(w["a"], toward) >= _dot(w["b"], toward) else tops[1]    # tall as its near end
            box("wall", [_dot(c, right) for c in quad], w["z0"], near)
            continue
        t = math.copysign(w["thickness"] / 2.0, facing_n)

        def at(s, z, w=w, t=t):
            return uz(wall_point(w, s, t), z)

        def top(s, w=w, length=length):
            return w["top_a"] + (w["top_b"] - w["top_a"]) * s / length
        shapes.append(("wall", [at(0.0, w["z0"]), at(length, w["z0"]), at(length, top(length)), at(0.0, top(0.0))],
                       True))
        if abs(tops[0] - tops[1]) < 1e-6:
            u0, u1 = sorted((at(0.0, 0.0)[0], at(length, 0.0)[0]))
            edges.append((w["id"], "top", u0, u1, tops[0]))
        for o in w.get("openings", []):
            s0, s1, z0, z1 = o["at"], o["at"] + o["width"], o["sill_z"], o["head_z"]
            kind = o.get("kind")
            colour = "window" if kind == "window" else "door" if kind == "door" else "cased"
            shapes.append((colour, [at(s0, z0), at(s1, z0), at(s1, z1), at(s0, z1)], True))
            u0, u1 = sorted((at(s0, 0.0)[0], at(s1, 0.0)[0]))
            edges.append((o["id"], "head", u0, u1, z1))
            if z0 > w["z0"] + 1e-6:
                edges.append((o["id"], "sill", u0, u1, z0))
            labels.append((o["id"], ((u0 + u1) / 2.0, (z0 + z1) / 2.0)))
    outdoor = {z for z, v in doc.get("zones", {}).items() if v.get("outdoor")}
    for e in els:
        t = e.get("type")
        if t == "roof_plane":
            pts, th = e["pts"], e["thickness"]
            low = min(p[2] for p in pts)
            for dz, what in ((0.0, "top"), (-th, "underside")):
                ring = [uz(p, p[2] + dz) for p in pts]
                shapes.append(("roof", ring, True))
                # the level edges an elevation draws: the top's (an eave, a ridge) and the fascia's foot at the eave
                for (a, za), (b, zb) in zip(ring, ring[1:] + ring[:1]):
                    if abs(za - zb) < 1e-6 and abs(a - b) > 12.0 and (dz == 0.0 or abs(za - dz - low) < 1e-6):
                        edges.append((e["id"], what, min(a, b), max(a, b), za))
        elif e.get("zone") not in outdoor:
            continue
        elif t == "slab":
            box("slab", [_dot(p, right) for p in e["outline"]], e["z_top"] - e["thickness"], e["z_top"], e["id"],
                "top")
        elif t == "column":
            c = e["center"]
            half = (e["diameter"] / 2.0, e["diameter"] / 2.0) if e.get("shape") == "round" else \
                (e["size"][0] / 2.0, e["size"][1] / 2.0)
            reach = abs(right[0]) * half[0] + abs(right[1]) * half[1]
            box("column", [_dot(c, right) - reach, _dot(c, right) + reach], e["z0"], e["z1"])
        elif t == "beam":
            hw = e["width"] / 2.0
            us = [_dot(p, right) + k * hw * math.hypot(*right) for p in (e["a"], e["b"]) for k in (-1.0, 1.0)]
            box("over", us, e["soffit_z"], e["soffit_z"] + e["depth"], e["id"], "top")
            edges.append((e["id"], "soffit", min(us), max(us), e["soffit_z"]))
        elif t == "stair":
            us = [_dot(e["a"], right), _dot(e["b"], right)]
            for k in range(e["risers"]):
                z = e["z_top"] - k * e["rise"]
                shapes.append(("stair", [(min(us), z), (max(us), z)], False))
            box("stair", us, e["z_top"] - e["risers"] * e["rise"], e["z_top"])
        elif t == "box":
            x0, y0, z0, x1, y1, z1 = e["bbox"]
            box("box", [_dot(p, right) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))], z0, z1, e["id"], "top")
    datums = {k: float(v) for k, v in doc.get("datums", {}).items() if isinstance(v, (int, float))}
    return {"shapes": shapes, "edges": edges, "labels": labels, "datums": datums}


def elevation_extent(cut):
    pts = [p for _, ring, _ in cut["shapes"] for p in ring]
    us, zs = [p[0] for p in pts], [p[1] for p in pts]
    return (min(us), min(zs), max(us), max(zs))


def height_misfits(edges, segs, search=3.0, min_overlap=0.3):
    """For each level model line (elevation_cut's edges), the offset of the nearest level sheet line (segs: pieces in
    (u, z) inches through an elevation's registration) within `search` inches, counting every piece at that height
    toward covering at least `min_overlap` of the line. Rows {id, face (what the line is), offset (sheet minus model,
    or None), a, b (its ends)}, as face_misfits gives them."""
    level = [s for s in segs if s["axis"] == "h"]
    rows = []
    for eid, what, u0, u1, z in edges:
        length = max(u1 - u0, 1e-9)
        cover = {}
        for s in level:
            off = (s["a"][1] + s["b"][1]) / 2.0 - z
            if abs(off) > search:
                continue
            a, b = sorted((s["a"][0], s["b"][0]))
            overlap = min(b, u1) - max(a, u0)
            if overlap > 0:
                key = round(off * 20) / 20
                cover[key] = cover.get(key, 0.0) + overlap
        hits = sorted((abs(k), k) for k, v in cover.items() if v >= min_overlap * length)
        rows.append({"id": eid, "face": what, "offset": hits[0][1] if hits else None, "a": (u0, z), "b": (u1, z)})
    return rows


# ------------------------------------------------------------------ wall faces against the sheet

def face_misfits(walls, segs, search=3.0, min_overlap=0.3, max_turn_deg=2.0):
    """For each face of each cut wall, the offset of the nearest parallel sheet line (segs: pieces in model inches,
    extract.pdf.segments through a registration) within `search` inches, counting every piece at that offset toward
    covering at least `min_overlap` of the face. Rows {id, face, offset (sheet minus model, outward positive, or None),
    a, b (the face's ends)}."""
    sin_tol = math.sin(math.radians(max_turn_deg))
    rows = []
    for w in walls:
        u, n, length = wall_frame(w)
        if length < 12.0:
            continue
        for face, side in (("left", 1.0), ("right", -1.0)):
            t = side * w["thickness"] / 2.0
            p = wall_point(w, 0.0, t)
            out_n = (n[0] * side, n[1] * side)
            cover = {}
            for s in segs:
                d = (s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
                sl = math.hypot(*d)
                if abs(u[0] * d[1] - u[1] * d[0]) > sin_tol * sl:
                    continue
                mid = ((s["a"][0] + s["b"][0]) / 2 - p[0], (s["a"][1] + s["b"][1]) / 2 - p[1])
                off = mid[0] * out_n[0] + mid[1] * out_n[1]
                if abs(off) > search:
                    continue
                along = sorted(((q[0] - p[0]) * u[0] + (q[1] - p[1]) * u[1]) for q in (s["a"], s["b"]))
                overlap = min(along[1], length) - max(along[0], 0.0)
                if overlap > 0:
                    key = round(off * 20) / 20           # pieces within 0.05 in are one line
                    cover[key] = cover.get(key, 0.0) + overlap
            hits = sorted((abs(k), k) for k, v in cover.items() if v >= min_overlap * length)
            rows.append({"id": w["id"], "face": face, "offset": hits[0][1] if hits else None,
                         "a": p, "b": wall_point(w, length, t)})
    return rows


# ------------------------------------------------------------------ drawing

class Canvas:
    """An RGBA layer over an image, drawing model-space geometry through to_px (model inches -> pixels)."""

    def __init__(self, base, to_px, px_per_in):
        self.base = base.convert("RGBA")
        self.layer = Image.new("RGBA", self.base.size, (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.layer)
        self.to_px = to_px
        self.k = px_per_in
        self.width = max(1, round(px_per_in * 0.6))               # a line about 0.6 in of house wide
        self.font = ImageFont.load_default(size=max(10, min(18, round(px_per_in * 3.5))))
        self.small = ImageFont.load_default(size=max(9, min(14, round(px_per_in * 2.5))))

    def px(self, pts):
        return [self.to_px(*p) for p in pts]

    def poly(self, pts, colour, fill_alpha=0, line=True, dashed=False):
        q = self.px(pts)
        if fill_alpha:
            self.draw.polygon(q, fill=colour + (fill_alpha,))
        if line:
            self.line(pts + pts[:1], colour, dashed=dashed)

    def line(self, pts, colour, dashed=False, width=None):
        q = self.px(pts)
        w = width or self.width
        if not dashed:
            self.draw.line(q, fill=colour + (255,), width=w, joint="curve")
            return
        dash, gap = 6 * w + 4, 4 * w + 3
        for a, b in zip(q, q[1:]):
            d = math.dist(a, b)
            s = 0.0
            while s < d:
                e = min(s + dash, d)
                self.draw.line([(a[0] + (b[0] - a[0]) * s / d, a[1] + (b[1] - a[1]) * s / d),
                                (a[0] + (b[0] - a[0]) * e / d, a[1] + (b[1] - a[1]) * e / d)],
                               fill=colour + (255,), width=w)
                s = e + gap

    def text(self, at, words, colour, small=False):
        x, y = self.to_px(*at)
        font = self.small if small else self.font
        box = self.draw.textbbox((x, y), words, font=font, anchor="mm")
        self.draw.rectangle((box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill=(255, 255, 255, 190))
        self.draw.text((x, y), words, fill=colour + (255,), font=font, anchor="mm")

    def image(self):
        return Image.alpha_composite(self.base, self.layer).convert("RGB")


def arc(centre, radius, a0, a1, steps=16):
    return [(centre[0] + radius * math.cos(a0 + (a1 - a0) * i / steps),
             centre[1] + radius * math.sin(a0 + (a1 - a0) * i / steps)) for i in range(steps + 1)]


def draw_opening(cv, w, o, z):
    s0, s1 = o["at"], o["at"] + o["width"]
    h = w["thickness"] / 2.0
    kind = o.get("kind")
    colour = COLOURS["window" if kind == "window" else "door" if kind == "door" else "cased"]
    if not opening_cut(o, z):
        cv.poly(wall_quad(w, s0, s1), colour, dashed=True)
        return
    for s in (s0, s1):                                         # the jambs
        cv.line([wall_point(w, s, -h), wall_point(w, s, h)], colour)
    if kind == "window":
        cv.line([wall_point(w, s0, 0.0), wall_point(w, s1, 0.0)], colour)
    elif kind == "door":
        sw = o.get("swing")
        if not sw:
            cv.line([wall_point(w, s0, 0.0), wall_point(w, s1, 0.0)], colour)
            return
        side = 1.0 if sw.get("side") == "left" else -1.0
        u, n, _ = wall_frame(w)
        hinge = sw.get("hinge", "start")
        leaves = [(s0, s1)] if hinge == "start" else [(s1, s0)] if hinge == "end" else \
            [(s0, (s0 + s1) / 2), (s1, (s0 + s1) / 2)]
        for at, free in leaves:
            radius = abs(free - at)
            pivot = wall_point(w, at, side * h)
            tip = (pivot[0] + n[0] * side * radius, pivot[1] + n[1] * side * radius)
            cv.line([pivot, tip], colour)
            along = 1.0 if free > at else -1.0
            shut = math.atan2(u[1] * along, u[0] * along)
            opened = math.atan2(n[1] * side, n[0] * side)
            turn = (opened - shut + math.pi) % (2 * math.pi) - math.pi
            cv.line(arc(pivot, radius, shut, shut + turn), colour, width=max(1, cv.width // 2))


def draw_plan(cv, cut, z, ids=False):
    """The plan cut (plan_cut) drawn on a Canvas."""
    for s in cut["slabs"]:
        cv.poly([tuple(p) for p in s["outline"]], COLOURS["slab"], fill_alpha=40)
    for s in cut["spaces"]:
        cv.poly([tuple(p) for p in s["outline"]], COLOURS["space"], dashed=True)
    for b in cut["floor"]:
        x0, y0, _, x1, y1, _ = b["bbox"]
        cv.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], COLOURS["box"], fill_alpha=45)
    solid = walls_solid(cut["walls"], z)
    for g in getattr(solid, "geoms", [solid]):
        if g.geom_type == "Polygon" and not g.is_empty:
            cv.poly(list(g.exterior.coords)[:-1], COLOURS["wall"], fill_alpha=110)
            for ring in g.interiors:
                cv.line(list(ring.coords), COLOURS["wall"])
    for w in cut["walls"]:
        for o in w.get("openings", []):
            draw_opening(cv, w, o, z)
    for c in cut["columns"]:
        cx, cy = c["center"]
        if c.get("shape") == "round":
            r = c["diameter"] / 2.0
            cv.poly(arc((cx, cy), r, 0.0, 2 * math.pi, 24)[:-1], COLOURS["column"], fill_alpha=200)
        else:
            sx, sy = c["size"]
            cv.poly([(cx - sx / 2, cy - sy / 2), (cx + sx / 2, cy - sy / 2), (cx + sx / 2, cy + sy / 2),
                     (cx - sx / 2, cy + sy / 2)], COLOURS["column"], fill_alpha=200)
    for s in cut["stairs"]:
        a, b, d = s["a"], s["b"], s["descend"]
        for i in range(s["risers"]):
            k = i * s["run"]
            cv.line([(a[0] + d[0] * k, a[1] + d[1] * k), (b[0] + d[0] * k, b[1] + d[1] * k)], COLOURS["stair"])
        k = s["run"] * (s["risers"] - 1)
        for e in (a, b):
            cv.line([tuple(e), (e[0] + d[0] * k, e[1] + d[1] * k)], COLOURS["stair"])
    for b in cut["over"]:
        x0, y0, _, x1, y1, _ = b["bbox"]
        cv.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], COLOURS["over"], dashed=True)
    for b in cut["beams"]:
        beam = {"a": b["a"], "b": b["b"], "thickness": b["width"]}
        cv.poly(wall_quad(beam, 0.0, wall_frame(beam)[2]), COLOURS["over"], dashed=True)
    for s in cut["spaces"]:
        p = Polygon(s["outline"]).representative_point()
        cv.text((p.x, p.y), s.get("name") or s["id"], COLOURS["space"])
    if ids:
        for b in cut["floor"] + cut["over"]:
            x0, y0, _, x1, y1, _ = b["bbox"]
            cv.text(((x0 + x1) / 2, (y0 + y1) / 2), b["id"], COLOURS["box" if b in cut["floor"] else "over"], True)
        for w in cut["walls"]:
            for o in w.get("openings", []):
                cv.text(wall_point(w, o["at"] + o["width"] / 2, 0.0), o["id"], (0, 0, 0), True)


def draw_elevation(cv, cut, window, ids=False):
    """An elevation (elevation_cut) drawn on a Canvas, its datums as dashed lines across `window`."""
    datums = [(name, z) for name, z in sorted(cut["datums"].items(), key=lambda kv: kv[1])
              if window[1] <= z <= window[3]]
    for _, z in datums:
        cv.line([(window[0], z), (window[2], z)], COLOURS["datum"], dashed=True, width=max(1, cv.width // 2))
    for colour, ring, closed in cut["shapes"]:
        if closed:
            cv.poly(ring, COLOURS[colour], fill_alpha=30 if colour == "wall" else 0)
        else:
            cv.line(ring, COLOURS[colour])
    for name, z in datums:
        cv.text((window[0] + 72.0, z), f"{name} {z:g}", COLOURS["datum"], True)
    if ids:
        for eid, at in cut["labels"]:
            cv.text(at, eid, (0, 0, 0), True)


def legend(img, lines):
    """The image under a header strip: a title, then a key of (colour, words) in a row."""
    probe = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=14)
    items = [(COLOURS[k], words) for k, words in lines[1:]]
    widths = [28 + probe.textlength(t, font=font) + 14 for _, t in items]
    width = max(img.width, int(probe.textlength(lines[0], font=font)) + 16)
    rows, x = [[]], 8
    for item, w in zip(items, widths):           # wrap the key to the image's width
        if x + w > width and rows[-1]:
            rows.append([])
            x = 8
        rows[-1].append((item, x))
        x += w
    head = 28 + 20 * len(rows)
    out = Image.new("RGB", (width, img.height + head), (255, 255, 255))
    out.paste(img, (0, head))
    draw = ImageDraw.Draw(out)
    draw.text((8, 6), lines[0], fill=(0, 0, 0), font=font)
    for r, row in enumerate(rows):
        y = 26 + 20 * r
        for (c, t), x in row:
            draw.rectangle((x, y + 3, x + 16, y + 13), fill=c)
            draw.text((x + 22, y), t, fill=(0, 0, 0), font=font)
    draw.line((0, head - 1, width, head - 1), fill=(0, 0, 0))
    return out


KEY = [("wall", "wall cut"), ("window", "window"), ("door", "door, swing"), ("cased", "cased opening, pass"),
       ("space", "space"), ("box", "box on the floor"), ("over", "overhead: box, beam"), ("stair", "stair")]
ELEVATION_KEY = [("wall", "exterior wall"), ("window", "window"), ("door", "door"), ("cased", "cased opening, pass"),
                 ("roof", "roof plane, its thickness"), ("slab", "outdoor slab"), ("stair", "steps"),
                 ("over", "beam"), ("datum", "datum")]


def _view(doc, z, facing, depth, margin):
    """(what is drawn, its window in model inches, a function drawing it on a Canvas, its key, its title words, the
    rows' measure) for a plan cut at z, or the elevation of the facade facing `facing`."""
    if facing is None:
        cut = plan_cut(doc, z)
        box = _grown(extent(cut), margin)
        if box is None:
            raise drawings.DrawingError(f"the plan cut at z {z:g} draws nothing; is z inside a storey?")
        return (cut, box, lambda cv, window, ids: draw_plan(cv, cut, z, ids), KEY, f"cut at z {z:g}",
                lambda segs, search: face_misfits(cut["walls"], segs, search))
    if facing not in FACINGS:
        raise drawings.DrawingError(f"an elevation faces one of {', '.join(FACINGS)}, got {facing!r}")
    cut = elevation_cut(doc, facing, depth)
    if cut is None:
        raise drawings.DrawingError("the house has no exterior wall, so no facade to draw")
    return (cut, _grown(elevation_extent(cut), margin), lambda cv, window, ids: draw_elevation(cv, cut, window, ids),
            ELEVATION_KEY, f"elevation facing {facing}", lambda segs, search: height_misfits(cut["edges"], segs, search))


# ------------------------------------------------------------------ the two images

def load_house(path=HOUSE):
    path = Path(path)
    if not path.is_file():
        raise drawings.DrawingError(f"no {path}; compile the house first (uv run python look.py --compile-only)")
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()[:12]


def over_sheet(name, *, house=HOUSE, z=CUT_Z, facing=None, depth=DEPTH, dpi=150, window=None, layers=None,
               search=3.0, tol=1.0, ids=False, out=None, registrations=None, echo=print):
    """The plan cut at z, or with `facing` the elevation of that facade, over the registered sheet `name`; returns
    (the PNG's path, the measured rows, or None when nothing was measured)."""
    reg = drawings.registration(name, registrations)
    doc, sha = load_house(house)
    _, box, draw, key, title, measure = _view(doc, z, facing, depth, MARGIN)
    window = window or box
    geom = drawings.page(reg.sheet, reg.page)
    sheet = drawings.open_render(reg.sheet, reg.page, dpi)

    def sheet_px(x, y):
        return geom.to_px(*reg.to_pt(x, y), dpi)

    corners = [sheet_px(x, y) for x in (window[0], window[2]) for y in (window[1], window[3])]
    left, top = max(0, int(min(c[0] for c in corners))), max(0, int(min(c[1] for c in corners)))
    right = min(sheet.width, int(math.ceil(max(c[0] for c in corners))))
    bottom = min(sheet.height, int(math.ceil(max(c[1] for c in corners))))
    if right - left < 4 or bottom - top < 4:
        raise drawings.DrawingError(f"the house lands outside {drawings.rel(reg.sheet)} page {reg.page} through "
                                    f"registration {name}: check its origin_pt and scale")
    if left > min(c[0] for c in corners) + 2 or right < max(c[0] for c in corners) - 2 or \
            top > min(c[1] for c in corners) + 2 or bottom < max(c[1] for c in corners) - 2:
        echo(f"note: part of the window lands off the sheet through registration {name}; the image is cut at the "
             "sheet's edge")
    base = sheet.crop((left, top, right, bottom)).convert("L").point(lambda v: 110 + v * 145 // 255).convert("RGB")
    cv = Canvas(base, lambda x, y: tuple(c - o for c, o in zip(sheet_px(x, y), (left, top))),
                dpi / 72.0 / reg.scale)
    draw(cv, window, ids)
    rows = None
    what = "heights" if facing else "wall faces"
    have = drawings.layer_names(reg.sheet)
    if layers:
        drawings.check_layers(reg.sheet, layers)
    elif not have:
        layers = ["None"]
        echo(f"note: the sheet has no CAD layers, so {what} are measured against every line on it; a dimension or "
             "border line can match too, so look at the image")
    else:
        echo(f"note: no {what} measured; give --layers with the sheet's {'elevation' if facing else 'wall'} layers "
             f"(it has {', '.join(have)})")
    if layers:
        segs = drawings.segments(reg.sheet, reg.page, layers=layers, reg=reg, min_length=3.0,
                                 window=(window[0], window[1], window[2], window[3]))
        rows = measure(segs, search)
        for r in rows:
            if r["offset"] is not None and abs(r["offset"]) > tol:
                mid = ((r["a"][0] + r["b"][0]) / 2, (r["a"][1] + r["b"][1]) / 2)
                cv.text(mid, f"{r['offset']:+.1f}", COLOURS["misfit"])
        _report(rows, tol, echo, what)
        if rows and all(r["offset"] is None for r in rows):
            echo(f"note: are the lines on {', '.join(layers)}? The sheet's layers: {', '.join(have) or 'none'}; and is "
                 f"registration {name} right (the image shows)?")
    img = legend(cv.image(), [f"{name}: house {sha} {title}"] + key)
    out = Path(out) if out else OUT / f"{name}{'' if facing or z == CUT_Z else f'-z{z:g}'}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out, rows


def plain(*, house=HOUSE, z=CUT_Z, facing=None, depth=DEPTH, px_per_ft=40.0, window=None, ids=False, out=None):
    """The plan cut at z, or with `facing` the elevation of that facade, alone on a one-foot grid, to scale; returns
    the PNG's path."""
    doc, sha = load_house(house)
    _, box, draw, key, title, _ = _view(doc, z, facing, depth, 24.0)
    window = window or box
    k = px_per_ft / 12.0
    x0, y0, x1, y1 = window
    size = (max(1, round((x1 - x0) * k)), max(1, round((y1 - y0) * k)))
    if size[0] * size[1] > 6e7:
        raise drawings.DrawingError(f"a {size[0]} x {size[1]} px drawing is too large; lower --px-per-ft")
    base = Image.new("RGB", size, (255, 255, 255))
    to_px = lambda x, y: ((x - x0) * k, (y1 - y) * k)            # noqa: E731
    grid = ImageDraw.Draw(base)
    font = ImageFont.load_default(size=12)
    names = ("u", "z") if facing else ("x", "y")
    for axis, (lo, hi) in enumerate(((x0, x1), (y0, y1))):
        v = math.ceil(lo / 12.0) * 12.0
        while v <= hi:
            five = round(v / 12.0) % 5 == 0
            a, b = (to_px(v, y0), to_px(v, y1)) if axis == 0 else (to_px(x0, v), to_px(x1, v))
            grid.line([a, b], fill=(200, 200, 200) if five else (235, 235, 235), width=1)
            if five:
                grid.text((a[0] + 2, size[1] - 16) if axis == 0 else (2, a[1] + 2), f"{names[axis]} {v:g}",
                          fill=(120, 120, 120), font=font)
            v += 12.0
    cv = Canvas(base, to_px, k)
    draw(cv, window, ids)
    img = legend(cv.image(), [f"house {sha} {title}; grid 1 ft"] + key)
    stem = f"elevation{facing}" if facing else "plan" + ("" if z == CUT_Z else f"-z{z:g}")
    out = Path(out) if out else OUT / f"{stem}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def _grown(box, by=MARGIN):
    return None if box is None else (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _report(rows, tol, echo, what="wall faces"):
    found = [r for r in rows if r["offset"] is not None]
    for r in sorted(rows, key=lambda r: -1.0 if r["offset"] is None else abs(r["offset"]), reverse=True):
        off = "no sheet line" if r["offset"] is None else f"{r['offset']:+6.2f} in"
        mark = "  " if r["offset"] is not None and abs(r["offset"]) <= tol else ">>" if r["offset"] is not None else "--"
        echo(f"{mark} {r['id']:36s} {r['face']:9s} {off}")
    if found:
        offs = [abs(r["offset"]) for r in found]
        echo(f"{len(found)} of {len(rows)} {what} matched a sheet line; median {statistics.median(offs):.2f} in, "
             f"worst {max(offs):.2f} in; {sum(o > tol for o in offs)} over {tol:g} in")
    else:
        echo(f"none of {len(rows)} {what} matched a sheet line on those layers")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, v in enumerate(argv[:-1]):            # "--elevation -y": argparse would take -y for an option
        if v == "--elevation" and argv[i + 1] in FACINGS:
            argv[i:i + 2] = [f"--elevation={argv[i + 1]}"]
            break
    ap = argparse.ArgumentParser(prog="tools.plancheck", description=__doc__.splitlines()[0])
    ap.add_argument("name", nargs="?", help="a registration in sources/plans/registrations.json")
    ap.add_argument("--plain", action="store_true", help="the plan or elevation alone on a grid, with no sheet")
    ap.add_argument("--house", type=Path, default=HOUSE)
    ap.add_argument("--z", type=float, default=CUT_Z, help="the cut's absolute height, inches (default 48)")
    ap.add_argument("--elevation", choices=sorted(FACINGS), metavar="FACING",
                    help="draw the elevation of the facade facing -y, +y, -x or +x instead of a plan")
    ap.add_argument("--depth", type=float, default=DEPTH,
                    help="an elevation's exterior walls this far behind its front still draw, inches (default 36)")
    ap.add_argument("--dpi", type=float, default=150, help="the sheet's render resolution")
    ap.add_argument("--px-per-ft", type=float, default=40.0, help="--plain's resolution")
    ap.add_argument("--window", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                    help="model inches to show: x, y for a plan; u, z for an elevation")
    ap.add_argument("--layers", nargs="+", help="the sheet's wall layers, to measure wall faces against (a sheet "
                                                "with no CAD layers needs none)")
    ap.add_argument("--search", type=float, default=3.0, help="how far from a model line to look for the sheet's, "
                                                                "inches")
    ap.add_argument("--tol", type=float, default=1.0, help="offsets over this are written on the image, inches")
    ap.add_argument("--ids", action="store_true", help="label boxes and openings with their ids")
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.plain:
            path = plain(house=a.house, z=a.z, facing=a.elevation, depth=a.depth, px_per_ft=a.px_per_ft,
                         window=a.window, ids=a.ids, out=a.out)
        elif a.name:
            path, _ = over_sheet(a.name, house=a.house, z=a.z, facing=a.elevation, depth=a.depth, dpi=a.dpi,
                                 window=a.window, layers=a.layers, search=a.search, tol=a.tol, ids=a.ids, out=a.out)
        else:
            ap.error("name a registration, or give --plain")
    except drawings.DrawingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(drawings.rel(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
