"""The example cottage's synthetic sources, drawn from its house.json: a vector drawing set and one photo with picks,
so the drawing and photo tools have something to run on before a real house replaces the cottage.

    uv run python tests/fixtures/example_sources.py

Writes
- sources/plans/example-cottage.pdf: page 0 the floor plan (sheet A1), page 1 the front elevation (A2), both at
  1/4" = 1'-0" on tabloid paper, each line on a named CAD layer, with dimension strings, room names and a north arrow
- sources/plans/registrations.json: plan_main (page 0) and elev_front (page 1), as extract.pdf calibrate --register
  writes them
- sources/photos/living-01.jpg: the living room seen from beside the kitchen opening, toward the front door and the
  west window, shaded flat from the palette's colours
- overlay/picks/living-01.json: picks on that photo, read where their features land in it
and then solves the photo's camera (overlay.solve), which writes overlay/cameras/ and overlay/requests/.

Drawn from the model, these sources match it exactly: they show how the tools work, not how well anything was
measured. A real house deletes them with the rest of the cottage (CLAUDE.md, "Starting a new house").
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pypdf
from PIL import Image, ImageDraw, ImageFilter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, TextStringObject
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:                    # run as a script
    sys.path.insert(0, str(ROOT))

from extract.pdf import ftin  # noqa: E402
from ir.plan import plan_document  # noqa: E402
from overlay import features  # noqa: E402
from overlay.camera import Camera  # noqa: E402
from tools import plancheck  # noqa: E402

HOUSE = ROOT / "house.json"
SHEET = ROOT / "sources" / "plans" / "example-cottage.pdf"
REGISTRATIONS = ROOT / "sources" / "plans" / "registrations.json"
PHOTO = ROOT / "sources" / "photos" / "living-01.jpg"
PICKS = ROOT / "overlay" / "picks" / "living-01.json"
PALETTE = ROOT / "build" / "palette.json"

PAGE = (1224.0, 792.0)               # tabloid, landscape, in points
PT_PER_IN = 1.5                      # 1/4" = 1'-0": a foot of house is 18 pt of paper
PLAN_ORIGIN = (300.0, 220.0)         # where the plan puts the model's origin, the west and front walls' outer corner
ELEV_ORIGIN = (300.0, 250.0)         # where the elevation puts u 0, z 0: that corner on the main floor


# ------------------------------------------------------------------ the drawing set

class Sheet:
    """One page's content stream, by CAD layer, drawn in model inches through the page's origin."""

    def __init__(self, origin):
        self.origin, self.ops = origin, {}

    def pt(self, x, y):
        return self.origin[0] + x * PT_PER_IN, self.origin[1] + y * PT_PER_IN

    def line(self, layer, pts, *, closed=False, dashed=False, width=0.5, raw=False):
        q = [p if raw else self.pt(*p) for p in pts]
        op = f"{width:g} w {'[3 2] 0 d' if dashed else '[] 0 d'} {q[0][0]:.3f} {q[0][1]:.3f} m "
        op += " ".join(f"{x:.3f} {y:.3f} l" for x, y in q[1:]) + (" h" if closed else "") + " S\n"
        self.ops.setdefault(layer, []).append(op)

    def text(self, layer, at, words, *, size=7.0, vertical=False, raw=False):
        """Words centred on `at`, along the page or up it."""
        x, y = at if raw else self.pt(*at)
        half = 0.25 * size * len(words)                 # about half the run's width in Helvetica
        c, s = (0.0, 1.0) if vertical else (1.0, 0.0)
        x, y = x - c * half + s * size * 0.35, y - s * half - c * size * 0.35
        esc = words.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.setdefault(layer, []).append(f"BT /F1 {size:g} Tf {c:g} {s:g} {-s:g} {c:g} {x:.3f} {y:.3f} Tm "
                                              f"({esc}) Tj ET\n")

    def dimensions(self, stations, fixed, axis, *, ext_from):
        """A dimension string along x (axis 'x', at y = fixed) or y (at x = fixed): the line, a tick and an extension
        line at each station, and each span's length as the drawing writes it."""
        def p(s, f):
            return (s, f) if axis == "x" else (f, s)
        self.line("A-ANNO-DIMS", [p(stations[0], fixed), p(stations[-1], fixed)], width=0.3)
        out = 1.0 if fixed > ext_from else -1.0
        for s in stations:
            self.line("A-ANNO-DIMS", [p(s, ext_from + out * 3.0), p(s, fixed + out * 4.0)], width=0.3)
            a, b = self.pt(*p(s, fixed))
            self.line("A-ANNO-DIMS", [(a - 2.5, b - 2.5), (a + 2.5, b + 2.5)], width=0.8, raw=True)
        for s0, s1 in zip(stations, stations[1:]):
            self.text("A-ANNO-DIMS", p((s0 + s1) / 2.0, fixed + out * 4.0), ftin(s1 - s0), vertical=axis == "y")


def title_block(sheet, number, title):
    w, h = PAGE
    sheet.line("A-ANNO-TTLB", [(18, 18), (w - 18, 18), (w - 18, h - 18), (18, h - 18)], closed=True, width=1.0,
               raw=True)
    sheet.line("A-ANNO-TTLB", [(w - 230, 18), (w - 230, 150)], width=0.8, raw=True)
    sheet.line("A-ANNO-TTLB", [(w - 230, 150), (w - 18, 150)], width=0.8, raw=True)
    for i, (words, size) in enumerate((("EXAMPLE COTTAGE", 11), ("synthetic sheets for the worked example", 7),
                                       (f"{number}   {title}", 11), ("SCALE: 1/4\" = 1'-0\"", 8))):
        sheet.text("A-ANNO-TTLB", (w - 124, 128 - 26 * i), words, size=size, raw=True)


def plan_sheet(doc, planned):
    s = Sheet(PLAN_ORIGIN)
    cut = plancheck.plan_cut(doc, plancheck.CUT_Z)
    solid = plancheck.walls_solid(cut["walls"], plancheck.CUT_Z)
    for g in getattr(solid, "geoms", [solid]):
        for ring in [g.exterior, *g.interiors]:
            s.line("A-WALL", list(ring.coords)[:-1], closed=True, width=0.8)
    by_id = {e["id"]: e for e in planned}
    for w in cut["walls"]:
        _, n, _ = plancheck.wall_frame(w)
        h = w["thickness"] / 2.0
        for o in w["openings"]:
            s0, s1 = o["at"], o["at"] + o["width"]
            if o["kind"] == "window":
                if plancheck.opening_cut(o, plancheck.CUT_Z):
                    for t in (-0.75, 0.75):
                        s.line("A-GLAZ", [plancheck.wall_point(w, s0, t), plancheck.wall_point(w, s1, t)], width=0.3)
                else:                                        # a window over the cut, drawn dashed as plans do
                    s.line("A-GLAZ", plancheck.wall_quad(w, s0, s1), closed=True, dashed=True, width=0.3)
            elif o["kind"] == "door" and o.get("swing"):
                side = 1.0 if o["swing"]["side"] == "left" else -1.0
                hinge, free = (s0, s1) if o["swing"]["hinge"] == "start" else (s1, s0)
                pivot = plancheck.wall_point(w, hinge, side * h)
                tip = (pivot[0] + n[0] * side * o["width"], pivot[1] + n[1] * side * o["width"])
                s.line("A-DOOR", [pivot, tip], width=0.5)
                shut = plancheck.wall_point(w, free, side * h)
                a0 = math.atan2(shut[1] - pivot[1], shut[0] - pivot[0])
                a1 = math.atan2(tip[1] - pivot[1], tip[0] - pivot[0])
                turn = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
                s.line("A-DOOR", plancheck.arc(pivot, o["width"], a0, a0 + turn, 24), width=0.3)
    for b in cut["floor"] + cut["over"]:
        x0, y0, _, x1, y1, _ = b["bbox"]
        ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if b["kind"] == "appliance":
            s.line("A-EQPM", ring, closed=True)
            s.text("A-EQPM", ((x0 + x1) / 2, (y0 + y1) / 2), "REF", size=6)
        elif b["kind"] in ("counter", "upper_cabinet"):
            s.line("A-FLOR-CASE", ring, closed=True, dashed=b["kind"] == "upper_cabinet", width=0.5)
    for e in doc["elements"]:
        if e["type"] == "slab":
            s.line("A-FLOR-OTLN", [tuple(p) for p in e["outline"]], closed=True)
        elif e["type"] == "stair":
            a, b, d = e["a"], e["b"], e["descend"]
            for k in range(e["risers"]):
                r = k * e["run"]
                s.line("A-FLOR-STRS", [(a[0] + d[0] * r, a[1] + d[1] * r), (b[0] + d[0] * r, b[1] + d[1] * r)])
        elif e["type"] == "column":
            (cx, cy), (sx, sy) = e["center"], e["size"]
            s.line("A-COLS", [(cx - sx / 2, cy - sy / 2), (cx + sx / 2, cy - sy / 2), (cx + sx / 2, cy + sy / 2),
                              (cx - sx / 2, cy + sy / 2)], closed=True)
        elif e["type"] == "beam":
            s.line("A-BEAM", plancheck.wall_quad({"a": e["a"], "b": e["b"], "thickness": e["width"]}, 0.0,
                                                 math.dist(e["a"], e["b"])), closed=True, dashed=True, width=0.3)
        elif e["type"] == "space" and e.get("build_floor", True):
            c = Polygon(e["outline"]).representative_point()
            s.text("A-ANNO-TEXT", (c.x, c.y + 6), (e.get("name") or e["id"]).upper(), size=8)
            if e.get("ceiling_z") is not None:
                s.text("A-ANNO-TEXT", (c.x, c.y - 6), f"CLG {ftin(e['ceiling_z'] - e['floor_z'])}", size=6)
    # the dimension strings lib/frame.star's chains are read from, and the back wall's windows
    s.dimensions([0.0, 240.0, 360.0], -96.0, "x", ext_from=-72.0)
    s.dimensions([0.0, 360.0], -114.0, "x", ext_from=-72.0)
    s.dimensions([0.0, 132.0, 288.0], -30.0, "y", ext_from=0.0)
    s.dimensions([0.0, 288.0], -48.0, "y", ext_from=0.0)
    s.dimensions([0.0, 168.0, 288.0], 390.0, "y", ext_from=360.0)
    back = by_id["kitchen.wall.back"]["openings"]
    s.dimensions([0.0] + [v for o in back for v in (o["at"], o["at"] + o["width"])] + [360.0], 318.0, "x",
                 ext_from=288.0)
    s.line("A-ANNO-SYMB", [(470.0, 300.0), (470.0, 348.0)], width=0.8)          # the north arrow, up +y
    s.line("A-ANNO-SYMB", [(462.0, 332.0), (470.0, 348.0), (478.0, 332.0)], width=0.8)
    s.text("A-ANNO-SYMB", (470.0, 358.0), "N", size=10)
    title_block(s, "A1", "FLOOR PLAN")
    return s


def elevation_sheet(doc):
    s = Sheet(ELEV_ORIGIN)
    cut = plancheck.elevation_cut(doc, "-y")
    for colour, ring, closed in cut["shapes"]:
        s.line("A-ELEV", ring, closed=closed, width=0.8 if colour in ("wall", "roof") else 0.5)
    grade = doc["datums"]["grade"]
    s.line("A-ELEV", [(-60.0, grade), (420.0, grade)], width=1.2)
    for name, words in (("main_floor", "FIN. FLOOR"), ("wall_top", "T.O. PLATE"), ("ridge", "RIDGE")):
        z = doc["datums"][name]
        s.line("A-ANNO-DIMS", [(384.0, z), (402.0, z)], width=0.3)
        s.text("A-ANNO-DIMS", (430.0, z + 4.0), f"{words} {ftin(z)}", size=6)
    title_block(s, "A2", "FRONT ELEVATION")
    return s


def write_pdf(path, sheets):
    w = pypdf.PdfWriter()
    names = sorted({layer for s in sheets for layer in s.ops})
    ocgs = {n: w._add_object(DictionaryObject({NameObject("/Type"): NameObject("/OCG"),
                                               NameObject("/Name"): TextStringObject(n)})) for n in names}
    font = w._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                           NameObject("/Subtype"): NameObject("/Type1"),
                                           NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for s in sheets:
        page = w.add_blank_page(*PAGE)
        tags = {f"oc{i}": layer for i, layer in enumerate(names) if layer in s.ops}
        body = "q 0 0 0 RG 0 0 0 rg\n" + "".join(f"/OC /{t} BDC\n{''.join(s.ops[layer])}EMC\n"
                                                for t, layer in tags.items()) + "Q\n"
        content = DecodedStreamObject()
        content.set_data(body.encode("latin-1"))
        page[NameObject("/Contents")] = w._add_object(content)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Properties"): DictionaryObject({NameObject(f"/{t}"): ocgs[layer] for t, layer in tags.items()}),
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    w._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject(list(ocgs.values())),
        NameObject("/D"): DictionaryObject({NameObject("/Order"): ArrayObject(list(ocgs.values()))})})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        w.write(fh)


def registrations():
    sheet = SHEET.relative_to(ROOT).as_posix()
    return {
        "plan_main": {"sheet": sheet, "page": 0, "origin_pt": list(PLAN_ORIGIN), "scale": round(1 / PT_PER_IN, 8),
                      "rotation_deg": 0.0, "note": "A1, the floor plan: the west and front walls' outer corner"},
        "elev_front": {"sheet": sheet, "page": 1, "origin_pt": list(ELEV_ORIGIN), "scale": round(1 / PT_PER_IN, 8),
                       "rotation_deg": 0.0, "note": "A2, the front elevation (facing -y): u along +x, the west "
                                                    "corner at the main floor"},
    }


# ------------------------------------------------------------------ the photo

W, H = 1600, 1067
# In the room's back corner by the kitchen opening, level as listing photos are, looking 45 degrees between -x and -y.
EYE, TARGET, FOV = (230.0, 124.0, 58.0), (88.6, -17.4, 58.0), 55.0
SUPER = 2                                         # drawn at twice the size, then scaled down: smooth edges
NEAR = 1.0                                        # in: what is nearer the eye than this is cut away
SHADE = {(0, 0, 1): 1.0, (0, 0, -1): 0.80, (1, 0, 0): 0.90, (-1, 0, 0): 0.84, (0, 1, 0): 0.95, (0, -1, 0): 0.87}


class Face:
    """A planar polygon with holes, in model inches, its colour and the side it is seen from (its normal)."""

    def __init__(self, ring, normal, rgb, holes=(), alpha=255):
        self.ring, self.normal, self.rgb, self.holes, self.alpha = ring, normal, rgb, list(holes), alpha

    def shade(self):
        n = tuple(int(round(v)) for v in self.normal)
        k = SHADE.get(n, 0.88)
        return tuple(int(min(255, c * k)) for c in self.rgb)


def plane(wall, d0, d1, z0, z1, off):
    """The rectangle d0..d1 x z0..z1 of a wall's elevation, `off` from its centreline toward its left."""
    p = plancheck.wall_point
    return [(*p(wall, d0, off), z0), (*p(wall, d1, off), z0), (*p(wall, d1, off), z1), (*p(wall, d0, off), z1)]


def wall_groups(wall, palette, eye):
    """The faces of a planned wall, in drawing order, as groups (each its faces, near to the wall's own face): its two
    sides with the openings as holes, then each opening's reveals, unit and casing."""
    _, n, length = plancheck.wall_frame(wall)
    t = wall["thickness"] / 2.0
    groups = []
    for side, key in ((1.0, "finish_left"), (-1.0, "finish_right")):
        normal = (n[0] * side, n[1] * side, 0.0)
        mid = plancheck.wall_point(wall, length / 2.0, side * t)
        if (eye[0] - mid[0]) * normal[0] + (eye[1] - mid[1]) * normal[1] <= 0:
            continue                                                   # seen from behind
        p = plancheck.wall_point
        ring = [(*p(wall, 0.0, side * t), wall["z0"]), (*p(wall, length, side * t), wall["z0"]),
                (*p(wall, length, side * t), wall["top_b"]), (*p(wall, 0.0, side * t), wall["top_a"])]
        holes = [plane(wall, *o["rect"], side * t) for o in wall["openings"]]
        faces = [Face(ring, normal, palette[wall[key]], holes)]
        for o in wall["openings"]:
            d0, d1, z0, z1 = o["rect"]
            along = (-n[1], n[0])
            for d, sign in ((d0, 1.0), (d1, -1.0)):                  # the jambs' reveals
                q = [(*p(wall, d, -t), z0), (*p(wall, d, t), z0), (*p(wall, d, t), z1), (*p(wall, d, -t), z1)]
                faces.append(Face(q, (along[0] * sign, along[1] * sign, 0.0), palette[wall["finish"]]))
            for z, up in ((z0, 1.0), (z1, -1.0)):
                if up > 0 and z <= wall["z0"] + 1e-6:
                    continue
                q = [(*p(wall, d0, -t), z), (*p(wall, d1, -t), z), (*p(wall, d1, t), z), (*p(wall, d0, t), z)]
                faces.append(Face(q, (0.0, 0.0, up), palette[wall["finish"]]))
            u = o.get("unit")
            if u:
                at = u["offset"] + side * u["depth"] / 2.0
                lights = [plane(wall, *light, at) for light in u["lights"]]
                faces.append(Face(plane(wall, d0, d1, z0, z1, at), normal, palette[u["finish"]], lights))
                if u["glazed"]:
                    faces += [Face(q, normal, palette["glass"], alpha=40) for q in lights]
                elif o["kind"] in ("door", "window"):
                    faces += [Face(q, normal, palette[u["leaf_finish"]]) for q in lights]
            c = o.get("casing")
            face = "left" if side > 0 else "right"
            if c and face in c["faces"]:
                at = side * (t + c["proud"])
                faces.append(Face(plane(wall, *c["outer"], at), normal, palette[c["finish"]],
                                  [plane(wall, d0, d1, z0, z1, at)]))
        groups.append(faces)
    return groups


def box_faces(bbox, rgb):
    x0, y0, z0, x1, y1, z1 = bbox
    return [Face([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0, 0, 1), rgb),
            Face([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0), rgb),
            Face([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0, 1, 0), rgb),
            Face([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], (-1, 0, 0), rgb),
            Face([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1, 0, 0), rgb)]


def scene(planned, palette, eye):
    """Every face the photo may see."""
    faces = []
    for e in planned:
        if e["type"] == "wall":
            for g in wall_groups(e, palette, eye):
                faces += g
        elif e["type"] == "space":
            ring = e["outline"]
            if e["build_floor"]:
                faces.append(Face([(x, y, e["floor_z"]) for x, y in ring], (0, 0, 1), palette[e["floor_finish"]]))
            if e["build_ceiling"] and e.get("ceiling_z") is not None:
                faces.append(Face([(x, y, e["ceiling_z"]) for x, y in ring], (0, 0, -1),
                                  palette[e["ceiling_finish"]]))
        elif e["type"] == "box":
            faces += box_faces(e["bbox"], palette[e["finish"]])
    return faces


def clip(cam_pts):
    """A polygon in camera coordinates cut to z >= NEAR (Sutherland-Hodgman)."""
    out = []
    for a, b in zip(cam_pts, np.roll(cam_pts, -1, axis=0)):
        ina, inb = a[2] >= NEAR, b[2] >= NEAR
        if ina:
            out.append(a)
        if ina != inb:
            out.append(a + (b - a) * (NEAR - a[2]) / (b[2] - a[2]))
    return np.array(out)


def to_px(cam, ring):
    """A ring's corners in the supersampled image (continuous pixels, origin at the top-left corner), or None."""
    c = clip(cam.to_camera_frame(np.array(ring, float)))
    if len(c) < 3:
        return None
    return [((cam.f * x / z + cam.cx) * SUPER, (cam.f * y / z + cam.cy) * SUPER) for x, y, z in c]


def raster(cam, face, px, size, width=None):
    """Where a face covers the supersampled image, as (slice of rows, slice of columns, mask, depth): the face filled
    (its holes left open), or its outline drawn `width` wide, and the camera depth of its plane at each pixel centre.
    None when it covers nothing."""
    xs, ys = [p[0] for p in px], [p[1] for p in px]
    pad = (width or 0) + 1
    x0, y0 = max(0, int(math.floor(min(xs))) - pad), max(0, int(math.floor(min(ys))) - pad)
    x1, y1 = min(size[0], int(math.ceil(max(xs))) + pad), min(size[1], int(math.ceil(max(ys))) + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    d = ImageDraw.Draw(mask)
    shifted = [(x - x0, y - y0) for x, y in px]
    if width:
        d.line(shifted + shifted[:1], fill=255, width=width)
    else:
        d.polygon(shifted, fill=255)
        for hole in face.holes:
            hp = to_px(cam, hole)
            if hp is not None:
                d.polygon([(x - x0, y - y0) for x, y in hp], fill=0)
    m = np.asarray(mask) > 0
    if not m.any():
        return None
    ring = cam.to_camera_frame(np.array(face.ring, float))
    n = np.zeros(3)                                    # the plane's normal (Newell), and its offset n . p = k
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        n += (a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1])
    k = float(n @ ring.mean(axis=0))
    u = ((np.arange(x0, x1) + 0.5) / SUPER - cam.cx) / cam.f
    v = ((np.arange(y0, y1) + 0.5) / SUPER - cam.cy) / cam.f
    with np.errstate(divide="ignore", invalid="ignore"):
        depth = k / (n[0] * u[None, :] + n[1] * v[:, None] + n[2])
    m &= np.isfinite(depth) & (depth > NEAR / 2)
    return slice(y0, y1), slice(x0, x1), m, depth


def photo(planned, palette, cam):
    """The photo, shaded flat with a depth test at every pixel, so a face shows only where nothing stands nearer."""
    size = (W * SUPER, H * SUPER)
    sky = np.linspace(232, 196, size[1])[:, None]
    horizon = int(cam.cy * SUPER)                                # the camera is level
    rgb = np.zeros((size[1], size[0], 3))
    rgb[:] = np.stack([sky * 0.86, sky * 0.92, sky], axis=-1)
    rgb[max(0, horizon):] = (118, 132, 104)                      # the garden beyond the windows
    zbuf = np.full((size[1], size[0]), np.inf)
    seen = []
    for f in scene(planned, palette, cam.eye):
        c = np.mean(np.array(f.ring, float), axis=0)
        if np.dot(np.subtract(cam.eye, c), f.normal) > 0:        # seen from in front
            px = to_px(cam, f.ring)
            if px is not None:
                seen.append((f, px))
    for f, px in seen:                                           # opaque faces, nearest wins
        if f.alpha == 255 and (r := raster(cam, f, px, size)):
            rows, cols, m, depth = r
            m = m & (depth < zbuf[rows, cols])
            zbuf[rows, cols][m] = depth[m]
            rgb[rows, cols][m] = f.shade()
    for f, px in seen:                                           # their outlines, where the face is the nearest
        if f.alpha == 255 and (r := raster(cam, f, px, size, width=2)):
            rows, cols, m, depth = r
            m = m & (depth <= zbuf[rows, cols] * 1.002 + 0.05)
            rgb[rows, cols][m] = tuple(int(c * 0.72) for c in f.shade())
    clear = [(f, px) for f, px in seen if f.alpha < 255]         # glass over what lies behind it, far to near
    clear.sort(key=lambda t: -math.dist(cam.eye, np.mean(np.array(t[0].ring, float), axis=0)))
    for f, px in clear:
        if r := raster(cam, f, px, size):
            rows, cols, m, depth = r
            m = m & (depth < zbuf[rows, cols])
            a = f.alpha / 255.0
            rgb[rows, cols][m] = rgb[rows, cols][m] * (1 - a) + np.array(f.shade(), float) * a
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    out = img.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.6))
    noise = np.random.default_rng(7).normal(0.0, 2.5, (H, W, 3))
    return Image.fromarray(np.clip(np.asarray(out, float) + noise, 0, 255).astype(np.uint8))


# What each pick aims at: (label, reference, free axis, offset, role). The casings' inner edges are what the photo
# shows of the openings, so those picks name them (casing inner) rather than the openings' own jambs.
PICK_LIST = [
    ("inside corner, front and west walls, at the floor",
     [{"el": "living.wall.front", "face": "left"}, {"el": "living.wall.west", "face": "+x"},
      {"el": "living.space", "level": "floor"}], None, None, "solve"),
    ("inside corner, front and west walls, at the ceiling",
     [{"el": "living.wall.front", "face": "left"}, {"el": "living.wall.west", "face": "+x"},
      {"el": "living.space", "level": "ceiling"}], None, None, "solve"),
    ("ceiling line along the front wall", [{"el": "living.wall.front", "face": "left"},
                                           {"el": "living.space", "level": "ceiling"}], "x", None, "solve"),
    ("floor line along the west wall", [{"el": "living.wall.west", "face": "+x"},
                                        {"el": "living.space", "level": "floor"}], "y", None, "solve"),
]
for _el, _what, _face in (("living.wall.front.window", "front window", "left"),
                          ("living.wall.west.window", "west window", "+x")):
    for _jamb in ("start", "end"):
        for _level in ("sill", "head"):
            PICK_LIST.append((f"{_what}: casing's inner corner, {_jamb} jamb at the {_level}",
                              {"el": _el, "jamb": _jamb, "level": _level, "face": _face, "casing": "inner"}, None,
                              None, "solve"))
_DOOR = {"el": "living.wall.front.door", "face": "left"}
PICK_LIST += [
    ("front door: casing's inner corner, end jamb at the head",
     {**_DOOR, "jamb": "end", "level": "head", "casing": "inner"}, None, None, "solve"),
    ("front door: casing's inner corner, start jamb at the head",
     {**_DOOR, "jamb": "start", "level": "head", "casing": "inner"}, None, None, "check"),
    ("front door: casing's outer edge, end jamb at the floor",
     [{**_DOOR, "jamb": "end", "casing": "outer"}, {"el": "living.space", "level": "floor"}], None, None, "solve"),
]


def picks(cam):
    house = features.House.load(HOUSE)
    rng = np.random.default_rng(3)
    points = []
    for label, ref, free, offset, role in PICK_LIST:
        r = features.resolve(house, ref, free=free, offset=offset, where=label)
        model = np.array([v if v is not None else 0.0 for v in r.model])
        if free:                                     # a line pick: aimed at the middle of the line's stretch in view
            k = "xyz".index(free)
            seen = []
            for v in np.arange(0.0, 360.0, 3.0):
                model[k] = v
                uv, z = cam.project(model, return_depth=True)
                if z > NEAR and 60 < uv[0] < W - 60 and 60 < uv[1] < H - 60:
                    seen.append(v)
            model[k] = seen[len(seen) // 2] if seen else 0.0
        uv, z = cam.project(model, return_depth=True)
        if z <= NEAR or not (20 < uv[0] < W - 20 and 20 < uv[1] < H - 20):
            raise SystemExit(f"{label} is out of the photo ({uv}, depth {z}); move the camera or drop the pick")
        uv = uv + rng.normal(0.0, 0.4, 2)
        p = {"label": label, "px": [round(float(uv[0]), 1), round(float(uv[1]), 1)], "ref": ref, "sigma_px": 1.5}
        if free:
            p["free"] = free
        if offset:
            p["offset"] = list(offset)
        if role != "solve":
            p["role"] = role
        points.append(p)
    return {"photo": PHOTO.name,
            "notes": ["The living room from beside the kitchen opening, toward the front door and the west window "
                      "(a synthetic photo of the example cottage)"],
            "points": points}


def main():
    doc = json.loads(HOUSE.read_text())
    if doc["meta"]["project"]["name"] != "Example cottage":
        raise SystemExit("house.json is not the example cottage; these sources are the cottage's")
    plan = plan_document(doc)
    assert plan.ok, plan.message()
    write_pdf(SHEET, [plan_sheet(doc, plan.elements), elevation_sheet(doc)])
    REGISTRATIONS.write_text(json.dumps(registrations(), indent=1) + "\n")
    palette = {k: tuple(v["rgb"]) for k, v in json.loads(PALETTE.read_text()).items()}
    cam = Camera.look_at((W, H), EYE, TARGET, fov_v_deg=FOV)
    PHOTO.parent.mkdir(parents=True, exist_ok=True)
    photo(plan.elements, palette, cam).save(PHOTO, quality=92)
    PICKS.write_text(json.dumps(picks(cam), indent=1) + "\n")
    print(f"wrote {SHEET.relative_to(ROOT)}, {REGISTRATIONS.relative_to(ROOT)}, {PHOTO.relative_to(ROOT)} and "
          f"{PICKS.relative_to(ROOT)}")
    return subprocess.run([sys.executable, "-m", "overlay.solve", "living-01"], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
