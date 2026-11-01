"""Reading architect drawings: what a PDF holds, its pages as images, its CAD lines and text, and its scale.

    uv run python -m extract.pdf info PDF
    uv run python -m extract.pdf render PDF [--page N] [--dpi 150]
    uv run python -m extract.pdf layers PDF [--page N]
    uv run python -m extract.pdf segments PDF [--page N] [--layer NAME ...] [--window X0 Y0 X1 Y1]
                                          [--registration NAME] [--axis h|v] [--min-length L] [--max-length L]
                                          [--merge GAP]
    uv run python -m extract.pdf text PDF [--page N] [--window X0 Y0 X1 Y1] [--match REGEX] [--registration NAME]
    uv run python -m extract.pdf grid PDF [--page N] (--window X0 Y0 X1 Y1 | --center X Y --size W [H])
                                      [--units pt|px] [--dpi 300] [--zoom Z] [--step S] [--mark X Y ...]
    uv run python -m extract.pdf calibrate PDF [--page N] --a X Y --b X Y --real 12'-6" [--check X Y X Y REAL ...]
                                           [--units pt|px --dpi D] [--a-model X Y [--rotation DEG] [--register NAME]]

Pages count from 0, as `info` lists them: a PDF viewer's page 3 is --page 2. Coordinates are PDF points (1/72 in of
paper) in the page's user space: origin at the bottom-left, y up, the page's /Rotate not applied. Vector lines (pdfvec), text positions and windows all use it, and so do scans: a scanned sheet is
read with `grid` (whose labels can be points) and calibrated in points, so one registration format serves both.
`--units px` reads pixels of the image `render` writes at a dpi instead (origin top-left, y down, as an image viewer
shows it).

A registration maps a sheet into the model frame. sources/plans/registrations.json holds them by name:

    {"plan_main": {"sheet": "sources/plans/plans.pdf", "page": 1, "origin_pt": [x, y], "scale": 0.6667,
                   "rotation_deg": 0.0, "note": "what anchors it"}}

model = R(rotation_deg) * (pt - origin_pt) * scale, in inches: origin_pt is the sheet point of the model's origin,
scale the inches of house per point of paper (0.6667 at 1/4 in = 1 ft). `calibrate --a-model X Y --register NAME`
writes an entry (making the file the first time) and prints the line that mirrors it in lib/frame.star, where Starlark
cites points read off the sheet (tests/test_pdf.py holds the two copies together). Renders and grids are cached under
out/pdf/.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "pdf"
REGISTRATIONS = ROOT / "sources" / "plans" / "registrations.json"
LIST_LIMIT = 300                     # lines a listing prints unless --limit says otherwise
AXIS_TOL_DEG = 0.5                   # a piece this close to an axis runs along it
# Architectural scales as inches of house per inch of paper: 1/4 in = 1 ft is 48.
SCALES = {"3 in = 1 ft": 4, "1-1/2 in = 1 ft": 8, "1 in = 1 ft": 12, "3/4 in = 1 ft": 16, "1/2 in = 1 ft": 24,
          "3/8 in = 1 ft": 32, "1/4 in = 1 ft": 48, "3/16 in = 1 ft": 64, "1/8 in = 1 ft": 96,
          "3/32 in = 1 ft": 128, "1/16 in = 1 ft": 192, "1 in = 10 ft": 120, "1 in = 20 ft": 240,
          "1 in = 30 ft": 360, "1 in = 40 ft": 480}


class DrawingError(ValueError):
    """A request this module cannot answer, in words for the person or session asking."""


# ------------------------------------------------------------------ pages

@dataclass(frozen=True)
class Page:
    """A page's geometry as pdfium renders it: the crop box in points and the page's /Rotate."""
    path: Path
    index: int
    box: tuple[float, float, float, float]          # left, bottom, right, top (user space, points)
    rotation: int                                   # 0, 90, 180 or 270, clockwise as displayed

    @property
    def size_pt(self):
        """(width, height) as displayed, in points."""
        w, h = self.box[2] - self.box[0], self.box[3] - self.box[1]
        return (h, w) if self.rotation in (90, 270) else (w, h)

    def to_px(self, x, y, dpi):
        """A user-space point -> (column, row) in the page rendered at dpi."""
        s = dpi / 72.0
        left, bottom, right, top = self.box
        if self.rotation == 90:
            return ((y - bottom) * s, (x - left) * s)
        if self.rotation == 180:
            return ((right - x) * s, (y - bottom) * s)
        if self.rotation == 270:
            return ((top - y) * s, (right - x) * s)
        return ((x - left) * s, (top - y) * s)

    def to_pt(self, px, py, dpi):
        """(column, row) in the page rendered at dpi -> a user-space point."""
        k = 72.0 / dpi
        left, bottom, right, top = self.box
        if self.rotation == 90:
            return (left + py * k, bottom + px * k)
        if self.rotation == 180:
            return (right - px * k, bottom + py * k)
        if self.rotation == 270:
            return (right - py * k, top - px * k)
        return (left + px * k, top - py * k)


def _document(pdf):
    import pypdfium2 as pdfium
    path = Path(pdf)
    if not path.is_file():
        raise DrawingError(f"no PDF at {path}")
    return pdfium.PdfDocument(str(path))


def page(pdf, index=0) -> Page:
    doc = _document(pdf)
    try:
        if not 0 <= index < len(doc):
            raise DrawingError(f"{pdf} has {len(doc)} pages (0..{len(doc) - 1}); there is no page {index}")
        pg = doc[index]
        return Page(Path(pdf).resolve(), index, tuple(float(v) for v in pg.get_cropbox()), pg.get_rotation() % 360)
    finally:
        doc.close()


def file_key(pdf) -> str:
    """A short digest of the file's bytes: a render cache that survives renames and never serves an old version."""
    return hashlib.sha256(Path(pdf).read_bytes()).hexdigest()[:12]


def cache_dir(pdf) -> Path:
    return OUT / f"{Path(pdf).stem.replace(' ', '_')}-{file_key(pdf)}"


def render(pdf, index=0, dpi=150) -> Path:
    """The page as a PNG at dpi, rendered once and cached by file content, page and dpi."""
    out = cache_dir(pdf) / f"p{index}-{dpi:g}dpi.png"
    if out.is_file():
        return out
    geom = page(pdf, index)
    w, h = (v * dpi / 72.0 for v in geom.size_pt)
    if w * h > 2.5e8:
        raise DrawingError(f"page {index} at {dpi} dpi is {w:.0f} x {h:.0f} px; use a lower dpi, or `grid` for a "
                           "detail")
    doc = _document(pdf)
    try:
        img = doc[index].render(scale=dpi / 72.0).to_pil().convert("RGB")
    finally:
        doc.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.png")
    img.save(tmp)
    tmp.replace(out)
    return out


def open_render(pdf, index=0, dpi=150):
    """The cached render as a PIL image."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None                   # sheets are large on purpose
    return Image.open(render(pdf, index, dpi)).convert("RGB")


# ------------------------------------------------------------------ registrations

@dataclass(frozen=True)
class Registration:
    """A sheet placed in the model frame (module doc)."""
    name: str
    sheet: Path
    page: int
    origin_pt: tuple[float, float]
    scale: float
    rotation_deg: float = 0.0

    def to_model(self, x, y):
        dx, dy = (x - self.origin_pt[0]) * self.scale, (y - self.origin_pt[1]) * self.scale
        c, s = math.cos(math.radians(self.rotation_deg)), math.sin(math.radians(self.rotation_deg))
        return (dx * c - dy * s, dx * s + dy * c)

    def to_pt(self, x, y):
        c, s = math.cos(math.radians(self.rotation_deg)), math.sin(math.radians(self.rotation_deg))
        dx, dy = x * c + y * s, -x * s + y * c
        return (self.origin_pt[0] + dx / self.scale, self.origin_pt[1] + dy / self.scale)


def registrations(path=None) -> dict:
    path = Path(path or REGISTRATIONS)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise DrawingError(f"{path} is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise DrawingError(f"{path} must hold an object of registrations by name")
    return data


def registration(name, path=None) -> Registration:
    path = Path(path or REGISTRATIONS)
    data = registrations(path)
    if name not in data:
        raise DrawingError(f"no registration {name!r} in {path}; it has {', '.join(sorted(data)) or 'none'} "
                           "(calibrate --a-model ... --register NAME writes one)")
    r = data[name]
    try:
        sheet = Path(r["sheet"])
        return Registration(name, sheet if sheet.is_absolute() else ROOT / sheet, int(r.get("page", 0)),
                            (float(r["origin_pt"][0]), float(r["origin_pt"][1])), float(r["scale"]),
                            float(r.get("rotation_deg", 0.0)))
    except (KeyError, TypeError, ValueError, IndexError) as e:
        raise DrawingError(f"registration {name!r} in {path} needs sheet, page, origin_pt [x, y], scale and "
                           f"rotation_deg: {e}") from None


def solve_registration(a_pt, a_model, scale, rotation_deg=0.0):
    """origin_pt for a registration in which the sheet point a_pt lands on the model point a_model."""
    c, s = math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg))
    mx, my = a_model
    dx, dy = mx * c + my * s, -mx * s + my * c          # R(-rotation) * model
    return (a_pt[0] - dx / scale, a_pt[1] - dy / scale)


# ------------------------------------------------------------------ vectors

@functools.lru_cache(maxsize=4)
def _paths(pdf, index, key):
    from extract import pdfvec
    return tuple(pdfvec.extract(pdf, index))


def paths(pdf, index=0):
    """pdfvec's paths for a page, cached while the file is unchanged."""
    return _paths(str(Path(pdf).resolve()), index, file_key(pdf))


def _crosses_window(a, b, window):
    """Whether the segment ab touches the rectangle window (x0, y0, x1, y1), by Liang-Barsky clipping."""
    x0, y0, x1, y1 = window
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def axis_of(a, b, tol_deg=AXIS_TOL_DEG):
    """'h' or 'v' for a piece along an axis (within tol_deg), else None."""
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    if min(ang, 180.0 - ang) <= tol_deg:
        return "h"
    if abs(ang - 90.0) <= tol_deg:
        return "v"
    return None


def layer_names(pdf) -> list:
    """The CAD layers (optional content groups) a PDF declares; [] for a PDF with none."""
    from extract import pdfvec
    return pdfvec.layer_names(str(pdf))


def check_layers(pdf, names):
    """DrawingError unless every name is a CAD layer of the PDF, or "None" (the lines on no layer, which is every line
    of a PDF with no layers)."""
    have = layer_names(pdf)
    unknown = [n for n in names if n != "None" and n not in have]
    if unknown:
        if not have:
            raise DrawingError(f"{rel(pdf)} has no CAD layers, so there is no layer {', '.join(unknown)}: every line is "
                               "on no layer (name it None, or leave the layers out)")
        raise DrawingError(f"{rel(pdf)} has no CAD layer {', '.join(unknown)}; its layers are {', '.join(have)} "
                           "(None: the lines on no layer)")


def segments(pdf, index=0, *, layers=None, window=None, reg: Registration | None = None, min_length=0.0,
             max_length=None, axis=None):
    """Every straight piece (two consecutive points of a path) on a page, each once: [{"layer", "a", "b", "length",
    "axis", "fill"}], in points or, through `reg`, in model inches. `window` (x0, y0, x1, y1) is in the same units and
    keeps the pieces that touch it, whole; `layers` keeps those layers (None for paths outside any layer: name it
    "None"); `max_length` drops longer pieces, such as a page border or a title block's frame. A piece drawn twice
    (a frame stroked on several layers, a line exported again as a thin filled outline) is listed once, the stroke
    kept over the fill."""
    want = None if layers is None else {None if n == "None" else n for n in layers}
    out, seen = [], {}
    for p in paths(pdf, index):
        if want is not None and p["layer"] not in want:
            continue
        pts = [reg.to_model(*q) for q in p["pts"]] if reg else p["pts"]
        for a, b in zip(pts, pts[1:]):
            length = math.dist(a, b)
            if length <= max(min_length, 1e-9) or (max_length is not None and length > max_length):
                continue
            if window is not None and not _crosses_window(a, b, window):
                continue
            ax = axis_of(a, b)
            if axis is not None and ax != axis:
                continue
            key = (p["layer"],) + tuple(sorted([(round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))]))
            if key in seen:
                if seen[key]["fill"] and not p["fill"]:
                    seen[key]["fill"] = False
                continue
            seen[key] = {"layer": p["layer"], "a": tuple(a), "b": tuple(b), "length": length, "axis": ax,
                         "fill": p["fill"]}
            out.append(seen[key])
    return out


def merge_collinear(pieces, gap):
    """Axis-aligned pieces of one layer on one line whose spans overlap or stand within `gap` joined into one: a
    dashed line, or a wall face broken at every junction, read as the one line it is. Other pieces pass through."""
    groups, rest = {}, []
    for s in pieces:
        if s["axis"] is None:
            rest.append(s)
            continue
        k = 1 if s["axis"] == "h" else 0              # the constant coordinate
        c = round((s["a"][k] + s["b"][k]) / 2.0, 2)
        lo, hi = sorted((s["a"][1 - k], s["b"][1 - k]))
        groups.setdefault((s["layer"], s["axis"], c), []).append((lo, hi, s["fill"]))
    out = list(rest)
    for (layer, ax, c), spans in groups.items():
        spans.sort()
        merged = []
        for lo, hi, fill in spans:
            if merged and lo <= merged[-1][1] + gap:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi, fill])
        for lo, hi, fill in merged:
            a, b = ((lo, c), (hi, c)) if ax == "h" else ((c, lo), (c, hi))
            out.append({"layer": layer, "a": a, "b": b, "length": hi - lo, "axis": ax, "fill": fill})
    return out


def layer_counts(pdf, index=0):
    """{layer: (pieces, total length in points)} on a page, most pieces first."""
    counts = {}
    for p in paths(pdf, index):
        n, length = counts.get(p["layer"], (0, 0.0))
        counts[p["layer"]] = (n + len(p["pts"]) - 1, length + sum(math.dist(a, b) for a, b in zip(p["pts"], p["pts"][1:])))
    return dict(sorted(counts.items(), key=lambda kv: -kv[1][0]))


# ------------------------------------------------------------------ text and dimensions

# Feet marks: ' and its typographic forms (right single quote, prime); inch marks: " and theirs, or two feet marks.
FOOT, INCH = "'\u2019\u2032", '"\u201d\u2033'
DIM_RE = re.compile(r"""
    (?:(?P<ft>\d+)\s*[""" + FOOT + r"""]\s*-?\s*)?            # feet: 12'
    (?:(?P<fnum>\d+)\s*/\s*(?P<fden>\d+)                 # a bare fraction: 1/2
       |(?P<inch>\d+(?:\.\d+)?)(?:[\s-]+(?P<num>\d+)\s*/\s*(?P<den>\d+))?)?   # or inches: 6, 6 1/2, 6-1/2
    \s*(?P<mark>[""" + INCH + r"""]|['\u2019]{2})?             # the inch mark
    """, re.X)


def find_dims(text):
    """Every feet-and-inches dimension in text, in inches: 12'-6 1/2" -> 150.5, 3'-0" -> 36, 7 1/2" -> 7.5, 12' ->
    144. A bare number is no dimension: it needs a foot or an inch mark."""
    out = []
    for m in DIM_RE.finditer(text):
        g = m.groupdict()
        has_inch = g["inch"] is not None or g["fnum"] is not None
        if not (g["ft"] or (has_inch and g["mark"])):
            continue
        inches = float(g["ft"] or 0) * 12.0
        if g["inch"] is not None:
            inches += float(g["inch"])
        if g["num"] and int(g["den"]):
            inches += int(g["num"]) / int(g["den"])
        if g["fnum"] and int(g["fden"]):
            inches += int(g["fnum"]) / int(g["fden"])
        out.append(inches)
    return out


def parse_dim(text):
    """A dimension given on its own (find_dims), in inches; a plain number is inches. DrawingError otherwise."""
    t = str(text).strip()
    try:
        return float(t)
    except ValueError:
        pass
    m = DIM_RE.fullmatch(t)
    dims = find_dims(t) if m else []
    if len(dims) != 1:
        raise DrawingError(f"{text!r} is no dimension: write inches (150.5) or feet and inches (12'-6 1/2\")")
    return dims[0]


def ftin(inches):
    """150.5 -> 12'-6 1/2" (to the nearest 1/16 in)."""
    sign = "-" if inches < 0 else ""
    n = round(abs(inches) * 16)
    ft, rest = divmod(n, 12 * 16)
    whole, frac = divmod(rest, 16)
    f = ""
    if frac:
        g = math.gcd(frac, 16)
        f = f" {frac // g}/{16 // g}"
    return f"{sign}{ft}'-{whole}{f}\""


QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u2032": "'", "\u201c": '"', "\u201d": '"', "\u2033": '"'})


# Joining characters into runs, as shares of a character's height (its font's ascent to descent). CAD plotters often
# write every character as its own text object, so the PDF's own grouping cannot be relied on. Along one baseline,
# characters closer than RUN_GAP are one run, and a gap wider than SPACE_GAP within a run is a space. Runs closer than
# WIDE_GAP are joined too (a label's wide spaces: SCALE: 1/2"  =  1'-0"), unless one ends in a dimension and the next
# starts with one, which are two strings of a dimension chain. A space is about 0.3 of the height; neighbouring
# strings of a chain can stand 0.6 apart.
RUN_GAP = 0.45
SPACE_GAP = 0.15
WIDE_GAP = 1.5
BASELINE_TOL = 0.25         # baselines closer than this share of the height are one line
SIZE_RATIO = 1.5            # characters whose heights differ by more than this factor are never one run


def _glyphs(tp):
    """Every drawn character of a text page, spaces left out: dicts of text, reading direction, height, baseline
    offset, extent along the direction, and tight box. pdfium gives a character's angle clockwise; the direction it
    reads in on the page is (cos a, -sin a)."""
    import ctypes
    import pypdfium2.raw as raw
    out = []
    ox, oy = ctypes.c_double(), ctypes.c_double()
    for i in range(tp.count_chars()):
        if raw.FPDFText_IsGenerated(tp.raw, i):
            continue
        ch = tp.get_text_range(i, 1)
        if not ch or ch.isspace() or ch == "\x00":
            continue
        a = raw.FPDFText_GetCharAngle(tp.raw, i)
        a = 0.0 if a < 0 else a
        deg = round(math.degrees(a)) % 360
        d = (math.cos(math.radians(deg)), -math.sin(math.radians(deg)))
        n = (-d[1], d[0])
        loose = tuple(float(v) for v in tp.get_charbox(i, loose=True))
        tight = tuple(float(v) for v in tp.get_charbox(i))
        raw.FPDFText_GetCharOrigin(tp.raw, i, ox, oy)
        # The loose box is the upright bounding box of the character's turned advance-by-height rectangle: undo the
        # turn to get its advance w and height h (ill-conditioned near 45 degrees, where the box's own projections
        # stand in).
        bw, bh = loose[2] - loose[0], loose[3] - loose[1]
        c, sn = abs(d[0]), abs(d[1])
        det = c * c - sn * sn
        if abs(det) > 0.3:
            w, h = (c * bw - sn * bh) / det, (c * bh - sn * bw) / det
        else:
            w, h = bw * c + bh * sn, bw * sn + bh * c
        if h <= 0:
            continue
        u0 = ox.value * d[0] + oy.value * d[1]
        if tight[2] <= tight[0] or tight[3] <= tight[1]:
            tight = loose
        out.append({"ch": ch, "deg": deg, "h": h, "v": ox.value * n[0] + oy.value * n[1],
                    "u0": u0, "u1": u0 + max(w, 0.0), "tight": tight})
    return out


def _edge_dims(text):
    """(whether text starts with a feet-and-inches dimension, whether it ends with one)."""
    starts = ends = False
    for m in DIM_RE.finditer(text):
        if m.group(0).strip() and find_dims(m.group(0)):
            starts = starts or not text[:m.start()].strip()
            ends = ends or not text[m.end():].strip()
    return starts, ends


def _run_text(run):
    """The glyphs of a run, in reading order, with a space at each gap wider than SPACE_GAP."""
    text, end = "", None
    for g in run:
        if end is not None and g["u0"] - end > SPACE_GAP * g["h"]:
            text += " "
        text += g["ch"]
        end = g["u1"] if end is None else max(end, g["u1"])
    return text.translate(QUOTES)


def _split(line, gap, keep_apart=None):
    """A baseline's glyph lists, joined where neighbours are closer than gap heights and of like size, and
    keep_apart(left text, right text) does not object."""
    out = [list(line[0])]
    for nxt in line[1:]:
        last = out[-1]
        h = min(nxt[0]["h"], last[-1]["h"])
        apart = (nxt[0]["u0"] - max(g["u1"] for g in last) > gap * h
                 or max(nxt[0]["h"], last[-1]["h"]) > SIZE_RATIO * h
                 or (keep_apart is not None and keep_apart(_run_text(last), _run_text(nxt))))
        if apart:
            out.append(list(nxt))
        else:
            last.extend(nxt)
    return out


def _runs_of(glyphs):
    """Glyphs joined into runs: one reading direction, one baseline, similar heights (RUN_GAP and WIDE_GAP above)."""
    def two_dimensions(left, right):
        return _edge_dims(left)[1] and _edge_dims(right)[0]

    runs = []
    by_dir = {}
    for g in glyphs:
        by_dir.setdefault(g["deg"], []).append(g)
    for deg, gs in by_dir.items():
        gs.sort(key=lambda g: g["v"])
        lines, line = [], [gs[0]]
        for g in gs[1:]:
            if g["v"] - line[-1]["v"] > BASELINE_TOL * min(g["h"], line[-1]["h"]):
                lines.append(line)
                line = []
            line.append(g)
        lines.append(line)
        for line in lines:
            line.sort(key=lambda g: g["u0"])
            # A label printed several times over itself (to look bold) is one label: drop a character that repeats
            # one just before it at the same place.
            kept = []
            for g in line:
                if not any(k["ch"] == g["ch"] and abs(k["u0"] - g["u0"]) < 0.15 * g["h"] for k in kept[-4:]):
                    kept.append(g)
            pieces = _split([[g] for g in kept], RUN_GAP)
            runs += [(deg, run) for run in _split(pieces, WIDE_GAP, two_dimensions)]
    out = []
    for deg, run in runs:
        box = (min(g["tight"][0] for g in run), min(g["tight"][1] for g in run),
               max(g["tight"][2] for g in run), max(g["tight"][3] for g in run))
        at = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        out.append({"text": _run_text(run), "box": box, "at": at, "angle": (-deg) % 360})
    return out


def text_runs(pdf, index=0):
    """The page's text as runs: [{"text", "box": (left, bottom, right, top), "at": centre, "angle"}], in points.
    Characters are joined by where they stand, not by how the PDF groups them (RUN_GAP above), so a dimension string a
    plotter wrote letter by letter reads whole. `angle` is the direction the run reads in, counter-clockwise from the
    page's +x: 0 left to right, 90 bottom to top. Typographic quotes and primes are written as ' and ". Empty for a scan
    (its lettering is an image: read it with `grid`)."""
    doc = _document(pdf)
    try:
        return _runs_of(_glyphs(doc[index].get_textpage()))
    finally:
        doc.close()


# ------------------------------------------------------------------ inventory

def page_info(pdf, index):
    """What a page holds, to tell a vector sheet from a scan: sizes, object counts, the largest image's share."""
    import pypdfium2.raw as raw
    doc = _document(pdf)
    try:
        pg = doc[index]
        w, h = pg.get_size()
        counts = {"path": 0, "text": 0, "image": 0}
        names = {raw.FPDF_PAGEOBJ_PATH: "path", raw.FPDF_PAGEOBJ_TEXT: "text", raw.FPDF_PAGEOBJ_IMAGE: "image"}
        biggest = 0.0
        for obj in pg.get_objects():
            kind = names.get(obj.type)
            if kind is None:
                continue
            counts[kind] += 1
            if kind == "image":
                left, bottom, right, top = obj.get_bounds()
                biggest = max(biggest, abs((right - left) * (top - bottom)) / (w * h))
        chars = pg.get_textpage().count_chars()
        rotation = pg.get_rotation()
    finally:
        doc.close()
    if biggest > 0.5 and counts["path"] < 500:
        kind = "scan"
    elif counts["path"] >= 50 or (counts["path"] >= 10 and chars):
        kind = "vector" if biggest < 0.5 else "vector over a scan"
    else:
        kind = "mostly empty" if not biggest else "images"
    return {"page": index, "size_pt": (w, h), "size_in": (w / 72.0, h / 72.0), "rotation": rotation,
            "objects": counts, "chars": chars, "largest_image": biggest, "kind": kind}


def nearest_scale(in_per_pt):
    """(name, its in_per_pt, the measured scale's deviation from it as a share) of the closest standard scale."""
    ratio = in_per_pt * 72.0
    name = min(SCALES, key=lambda k: abs(math.log(ratio / SCALES[k])))
    return name, SCALES[name] / 72.0, ratio / SCALES[name] - 1.0


# ------------------------------------------------------------------ grid images

def nice_step(span, lines=10):
    """A round step giving about `lines` grid lines across span: 1, 2 or 5 times a power of ten."""
    raw = span / max(lines, 1)
    p = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    return min((m * p for m in (1, 2, 5, 10)), key=lambda s: abs(s - raw))


def grid_image(pdf, index, window, *, units="pt", dpi=300, zoom=None, step=None, marks=(), out=None):
    """A crop of the page with a labelled grid, for reading coordinates off a drawing (a scan above all). window is
    (x0, y0, x1, y1) in points (user space) or in pixels of the page rendered at dpi. Marks are (x, y) in the same
    units, drawn as labelled crosshairs. Returns the PNG's path."""
    from PIL import Image, ImageDraw, ImageFont
    geom = page(pdf, index)
    img = open_render(pdf, index, dpi)
    if units == "pt":
        to_px = lambda x, y: geom.to_px(x, y, dpi)            # noqa: E731
    elif units == "px":
        to_px = lambda x, y: (x, y)                           # noqa: E731
    else:
        raise DrawingError(f"units are pt or px, got {units!r}")
    x0, y0, x1, y1 = window
    corners = [to_px(x, y) for x in (x0, x1) for y in (y0, y1)]
    left, top = max(0, int(min(c[0] for c in corners))), max(0, int(min(c[1] for c in corners)))
    right = min(img.width, int(math.ceil(max(c[0] for c in corners))))
    bottom = min(img.height, int(math.ceil(max(c[1] for c in corners))))
    if right - left < 2 or bottom - top < 2:
        raise DrawingError(f"the window {window} ({units}) lies outside page {index} ({img.width} x {img.height} px "
                           f"at {dpi} dpi)")
    crop = img.crop((left, top, right, bottom))
    if zoom is None:
        zoom = max(1.0, min(8.0, 1600.0 / max(crop.width, crop.height)))
    big = crop.resize((max(1, round(crop.width * zoom)), max(1, round(crop.height * zoom))), Image.LANCZOS)
    draw = ImageDraw.Draw(big, "RGBA")
    font = ImageFont.load_default(size=13)

    def at(x, y):
        px, py = to_px(x, y)
        return ((px - left) * zoom, (py - top) * zoom)

    step = step or nice_step(max(abs(x1 - x0), abs(y1 - y0)))
    for k, (lo, hi) in enumerate(((min(x0, x1), max(x0, x1)), (min(y0, y1), max(y0, y1)))):
        v = math.ceil(lo / step) * step
        while v <= hi + 1e-9:
            a, b = (at(v, y0), at(v, y1)) if k == 0 else (at(x0, v), at(x1, v))
            draw.line([a, b], fill=(0, 120, 255, 150), width=1)
            # labels along the top edge for upright lines and the left edge for level ones, clear of the corner
            if abs(a[0] - b[0]) < 1:
                spot = (a[0] + 2, 2) if a[0] > 1 else None
            else:
                spot = (2, a[1] + 2) if a[1] > 18 else None
            if spot:
                draw.text(spot, f"{'xy'[k]}={v:g}", fill=(0, 70, 200, 255), font=font)
            v += step
    for mx, my in marks:
        cx, cy = at(mx, my)
        draw.line([(cx - 14, cy), (cx + 14, cy)], fill=(230, 0, 0, 255), width=2)
        draw.line([(cx, cy - 14), (cx, cy + 14)], fill=(230, 0, 0, 255), width=2)
        draw.text((cx + 6, cy + 4), f"({mx:g}, {my:g})", fill=(200, 0, 0, 255), font=font)
    if out is None:
        out = cache_dir(pdf) / "grid" / f"p{index}-{units}-{x0:g}_{y0:g}_{x1:g}_{y1:g}-{dpi:g}dpi.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    big.save(out)
    return out


# ------------------------------------------------------------------ the command line

def rel(p):
    p = Path(p).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def _limited(lines, limit, hint):
    for ln in lines[:limit]:
        print(ln)
    if len(lines) > limit:
        print(f"... {len(lines) - limit} more ({hint}, or --limit)")


def cmd_info(a):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(a.pdf))
    n = len(doc)
    doc.close()
    print(f"{rel(a.pdf)}: {n} page{'s' if n != 1 else ''}")
    for i in range(n):
        p = page_info(a.pdf, i)
        o = p["objects"]
        turned = f", rotated {p['rotation']}" if p["rotation"] else ""
        images = f" (largest covers {p['largest_image']:.0%})" if o["image"] else ""
        print(f"  page {i}: {p['size_in'][0]:.1f} x {p['size_in'][1]:.1f} in ({p['size_pt'][0]:.0f} x "
              f"{p['size_pt'][1]:.0f} pt){turned}  {p['kind']}: {o['path']} paths, {o['text']} text objects "
              f"({p['chars']} chars), {o['image']} images{images}")
    names = layer_names(a.pdf)
    if names:
        shown = names[:80]
        print(f"  {len(names)} CAD layers: " + ", ".join(shown) + (" ..." if len(names) > len(shown) else ""))
    else:
        print("  no CAD layers (optional content) in this PDF: every line is on layer None")
    return 0


def cmd_render(a):
    print(rel(render(a.pdf, a.page, a.dpi)))
    return 0


def cmd_layers(a):
    counts = layer_counts(a.pdf, a.page)
    print(f"{rel(a.pdf)} page {a.page}: {sum(n for n, _ in counts.values())} pieces on {len(counts)} layers")
    for layer, (n, length) in counts.items():
        print(f"  {n:8d} pieces {length:10.0f} pt  {layer}")
    return 0


def _registration(a):
    if not getattr(a, "registration", None):
        return None
    reg = registration(a.registration)
    if reg.sheet.resolve() != Path(a.pdf).resolve() or reg.page != a.page:
        print(f"note: registration {reg.name} is of {rel(reg.sheet)} page {reg.page}", file=sys.stderr)
    return reg


def cmd_segments(a):
    reg = _registration(a)
    unit = "in" if reg else "pt"
    if a.layer:
        check_layers(a.pdf, a.layer)
    # with --merge, lengths are judged after joining: a wall face broken at a junction keeps its short pieces
    segs = segments(a.pdf, a.page, layers=a.layer, window=a.window, reg=reg,
                    min_length=0.0 if a.merge is not None else a.min_length, max_length=a.max_length, axis=a.axis)
    if a.merge is not None:
        segs = [s for s in merge_collinear(segs, a.merge) if s["length"] > a.min_length]
    if a.json:
        print(json.dumps(segs, indent=1))
        return 0
    segs.sort(key=lambda s: (str(s["layer"]), s["axis"] or "z", round(s["a"][1] if s["axis"] == "h" else s["a"][0], 2),
                             min(s["a"][0], s["b"][0]), min(s["a"][1], s["b"][1])))
    print(f"{len(segs)} pieces ({unit}{', through ' + reg.name if reg else ''})")
    lines = [f"  {str(s['layer'])[:28]:28s} {s['a'][0]:9.2f} {s['a'][1]:9.2f} -> {s['b'][0]:9.2f} {s['b'][1]:9.2f}  "
             f"len {s['length']:8.2f}  {s['axis'] or ''}{' fill' if s['fill'] else ''}" for s in segs]
    _limited(lines, a.limit, "narrow with --window, --layer, --axis or --min-length")
    return 0


def cmd_text(a):
    reg = _registration(a)
    runs = text_runs(a.pdf, a.page)
    if not runs:
        print("no text in this page's PDF (a scan: read its lettering with `grid`)")
        return 0
    rx = re.compile(a.match, re.I) if a.match else None
    rows = []
    for r in runs:
        at = reg.to_model(*r["at"]) if reg else r["at"]
        if a.window is not None and not (a.window[0] <= at[0] <= a.window[2] and a.window[1] <= at[1] <= a.window[3]):
            continue
        if rx and not rx.search(r["text"]):
            continue
        dims = find_dims(r["text"])
        rows.append({**r, "model_at": at if reg else None, "dims": dims})
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    rows.sort(key=lambda r: (-r["at"][1], r["at"][0]))
    print(f"{len(rows)} text runs (centre in pt{', and model in through ' + reg.name if reg else ''}; box w x h pt)")
    lines = []
    for r in rows:
        b = r["box"]
        where = f"{r['at'][0]:8.1f} {r['at'][1]:8.1f}"
        if reg:
            where += f"  model {r['model_at'][0]:8.1f} {r['model_at'][1]:8.1f}"
        dims = ("  = " + ", ".join(f"{d:g} in" for d in r["dims"])) if r["dims"] else ""
        turned = f"  (reads at {r['angle']} deg)" if r["angle"] else ""
        lines.append(f"  {where}  {b[2] - b[0]:5.1f} x {b[3] - b[1]:5.1f}  {r['text']}{dims}{turned}")
    _limited(lines, a.limit, "narrow with --window or --match")
    return 0


def cmd_grid(a):
    if a.window is not None:
        window = a.window
    elif a.center is not None and a.size is not None:
        w = a.size[0]
        h = a.size[1] if len(a.size) > 1 else w
        window = (a.center[0] - w / 2, a.center[1] - h / 2, a.center[0] + w / 2, a.center[1] + h / 2)
    else:
        raise DrawingError("grid needs --window X0 Y0 X1 Y1, or --center X Y with --size W [H]")
    if len(a.mark or []) % 2:
        raise DrawingError("--mark takes pairs: X Y [X Y ...]")
    marks = [tuple(a.mark[i:i + 2]) for i in range(0, len(a.mark or []), 2)]
    out = grid_image(a.pdf, a.page, window, units=a.units, dpi=a.dpi, zoom=a.zoom, step=a.step, marks=marks,
                     out=a.out)
    print(rel(out))
    return 0


def cmd_calibrate(a):
    geom = page(a.pdf, a.page)

    def pt(xy):
        return geom.to_pt(xy[0], xy[1], a.dpi) if a.units == "px" else (xy[0], xy[1])

    pa, pb = pt(a.a), pt(a.b)
    d = math.dist(pa, pb)
    if d <= 0:
        raise DrawingError("the two points are one point")
    real = parse_dim(a.real)
    scale = real / d
    name, nominal, off = nearest_scale(scale)
    print(f"a to b: {d:.3f} pt of paper for {ftin(real)} ({real:g} in): {scale:.6f} in per pt")
    print(f"nearest standard scale: {name} ({nominal:.6f} in per pt), measured {off:+.2%} from it"
          + ("" if abs(off) < 0.01 else "; a sheet printed to fit its paper keeps no standard scale, so trust the "
                                         "measured one and check it against more dimensions"))
    worst = 0.0
    for c in a.check or []:
        if len(c) != 5:
            raise DrawingError("--check takes X0 Y0 X1 Y1 REAL")
        q0, q1 = pt((float(c[0]), float(c[1]))), pt((float(c[2]), float(c[3])))
        want = parse_dim(c[4])
        got = math.dist(q0, q1) * scale
        worst = max(worst, abs(got - want))
        print(f"check: {ftin(want)} ({want:g} in) measures {got:.2f} in, off {got - want:+.2f} in "
              f"({(got - want) / want:+.2%})")
    if a.check:
        print(f"worst check: {worst:.2f} in")
    if a.a_model:
        origin = solve_registration(pa, a.a_model, scale, a.rotation)
        entry = {"sheet": rel(a.pdf), "page": a.page, "origin_pt": [round(origin[0], 4), round(origin[1], 4)],
                 "scale": round(scale, 8), "rotation_deg": a.rotation}
        print("registration: " + json.dumps(entry))
        if a.register:
            data = registrations()
            data[a.register] = {**data.get(a.register, {}), **entry}
            REGISTRATIONS.parent.mkdir(parents=True, exist_ok=True)
            REGISTRATIONS.write_text(json.dumps(data, indent=1) + "\n")
            rot = f", {a.rotation:g}" if a.rotation else ""
            print(f"wrote {a.register} to {rel(REGISTRATIONS)}. For Starlark to cite points read off this sheet, mirror "
                  f"it in lib/frame.star:\n    {a.register.upper()} = registration([{entry['origin_pt'][0]:g}, "
                  f"{entry['origin_pt'][1]:g}], {entry['scale']:.8g}{rot})")
    elif a.register:
        raise DrawingError("--register needs --a-model X Y: the model point that point a stands for")
    return 0


def _floats(n):
    return dict(nargs=n, type=float)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="extract.pdf", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def command(name, fn, help, page=True):
        p = sub.add_parser(name, help=help)
        p.add_argument("pdf", type=Path)
        if page:
            p.add_argument("--page", type=int, default=0, help="page index, from 0 (default 0)")
        p.set_defaults(fn=fn)
        return p

    command("info", cmd_info, "pages, sizes, vector or scan, and CAD layers", page=False)
    p = command("render", cmd_render, "a page as a PNG (cached under out/pdf/)")
    p.add_argument("--dpi", type=float, default=150)
    command("layers", cmd_layers, "pieces and length per CAD layer on a page")
    p = command("segments", cmd_segments, "straight pieces on a page, in points or model inches")
    p.add_argument("--layer", nargs="+", help="only these layers (None: paths on no layer, which in a PDF with no "
                                              "CAD layers is every path)")
    p.add_argument("--window", **_floats(4), metavar=("X0", "Y0", "X1", "Y1"), help="pieces touching this box")
    p.add_argument("--registration", help="report model inches through this registration")
    p.add_argument("--axis", choices=("h", "v"), help="only pieces along x (h) or y (v)")
    p.add_argument("--min-length", type=float, default=0.0, help="drop shorter pieces (hatching, text strokes)")
    p.add_argument("--max-length", type=float, help="drop longer pieces (the page border, a title block's frame)")
    p.add_argument("--merge", type=float, metavar="GAP", help="join collinear axis pieces of a layer within GAP "
                                                              "(before --min-length is applied)")
    p.add_argument("--limit", type=int, default=LIST_LIMIT)
    p.add_argument("--json", action="store_true")
    p = command("text", cmd_text, "text runs with positions, and the dimensions they read as")
    p.add_argument("--window", **_floats(4), metavar=("X0", "Y0", "X1", "Y1"))
    p.add_argument("--match", help="only runs matching this regular expression")
    p.add_argument("--registration", help="also give model inches, and read --window in them")
    p.add_argument("--limit", type=int, default=LIST_LIMIT)
    p.add_argument("--json", action="store_true")
    p = command("grid", cmd_grid, "a zoomed crop with a labelled grid, for reading coordinates")
    p.add_argument("--window", **_floats(4), metavar=("X0", "Y0", "X1", "Y1"))
    p.add_argument("--center", **_floats(2), metavar=("X", "Y"))
    p.add_argument("--size", nargs="+", type=float, metavar="W")
    p.add_argument("--units", choices=("pt", "px"), default="pt")
    p.add_argument("--dpi", type=float, default=300)
    p.add_argument("--zoom", type=float, help="enlargement of the crop (default: to about 1600 px)")
    p.add_argument("--step", type=float, help="grid spacing in units (default: about ten lines)")
    p.add_argument("--mark", nargs="+", type=float, metavar="X Y", help="crosshairs at these points")
    p.add_argument("-o", "--out", type=Path)
    p = command("calibrate", cmd_calibrate, "a scale from two points and the real distance between them")
    p.add_argument("--a", **_floats(2), required=True, metavar=("X", "Y"))
    p.add_argument("--b", **_floats(2), required=True, metavar=("X", "Y"))
    p.add_argument("--real", required=True, help="the real distance: 150.5 or 12'-6 1/2\"")
    p.add_argument("--check", nargs=5, action="append", metavar=("X0", "Y0", "X1", "Y1", "REAL"),
                   help="a second dimension to test the scale on (repeatable)")
    p.add_argument("--units", choices=("pt", "px"), default="pt")
    p.add_argument("--dpi", type=float, default=150, help="the render's dpi, for --units px")
    p.add_argument("--a-model", **_floats(2), metavar=("X", "Y"), help="the model point (inches) point a stands for")
    p.add_argument("--rotation", type=float, default=0.0, help="the sheet's turn into the model frame, degrees")
    p.add_argument("--register", metavar="NAME", help="write the registration to sources/plans/registrations.json")
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except DrawingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
