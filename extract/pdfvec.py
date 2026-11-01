"""Vector extraction from PDF pages, each path tagged with the CAD layer it was drawn on.

Architect drawings exported from CAD (AutoCAD, Revit, ArchiCAD, Vectorworks) usually keep their layers as PDF
optional-content groups (OCGs). This module interprets a page's content stream and returns every painted path as a
polyline in PDF user space (points, 1/72 in, origin at the bottom-left of the page, y up, the page's /Rotate not
applied), tagged with the layer that was active when it was painted.

    from extract import pdfvec
    segs = pdfvec.extract("sources/plans/plans.pdf", 0)   # [{"layer", "op", "pts", "lw", "color", "fill", "dash"}]
    pdfvec.layer_names("sources/plans/plans.pdf")         # every layer the document declares

Each item: `layer` (the OCG name, or None outside any layer), `op` (the painting operator), `pts` (the polyline,
[(x, y), ...]; a closed subpath repeats its first point), `lw` (line width in points), `color` (the stroke colour for
stroked paths, the fill colour for filled ones, RGB 0..1, or None), `fill` (whether the path was filled), `dash` (the
dash array). Beziers are flattened; form XObjects are followed with their own matrix, resources and layer. Text and
images are ignored (extract/pdf.py reads text through pdfium).
"""
from __future__ import annotations

import math
import re

import pypdf

TOK = re.compile(rb'''
    (?P<str>\((?:\\.|[^\\()])*\))
  | (?P<hex><[0-9A-Fa-f\s]*>)
  | (?P<dict_o><<) | (?P<dict_c>>>)
  | (?P<arr_o>\[) | (?P<arr_c>\])
  | (?P<name>/[^\s/\[\]<>(){}%]*)
  | (?P<num>[-+]?(?:\d+\.?\d*|\.\d+))
  | (?P<op>[A-Za-z'"*][A-Za-z0-9'"*]*)
''', re.X)
INLINE_END = re.compile(rb'\sEI(?=[\s/\[<(]|$)')   # the end of an inline image's binary data

PAINT_OPS = {'S', 's', 'f', 'F', 'f*', 'B', 'B*', 'b', 'b*', 'n'}
FILL_OPS = {'f', 'F', 'f*', 'B', 'B*', 'b', 'b*'}
STROKE_OPS = {'S', 's', 'B', 'B*', 'b', 'b*'}
MAX_DEPTH = 10                                      # form XObjects nested deeper than this are skipped


def mat_mul(a, b):
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


def xf(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def mat_scale(m):
    """The matrix's mean scale, for line widths and the flattening tolerance."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0


def flatten_cubic(p0, p1, p2, p3, tol=0.25):
    """A cubic Bezier as points after p0, more of them the longer its control net."""
    d = math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
    n = max(2, min(48, int(math.sqrt(d / max(tol, 1e-6)) * 1.5)))
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
                    u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]))
    return out


def _cmyk(c, m, y, k):
    return tuple(round((1 - v) * (1 - k), 3) for v in (c, m, y))


def _colour(nums):
    """A colour from a colour operator's numeric operands: gray, RGB or CMYK."""
    if len(nums) == 1:
        return (round(nums[0], 3),) * 3
    if len(nums) == 3:
        return tuple(round(v, 3) for v in nums)
    if len(nums) == 4:
        return _cmyk(*nums)
    return None


class Extractor:
    def __init__(self, path):
        self.reader = pypdf.PdfReader(path)

    @staticmethod
    def _ocg_label(obj):
        """An OCG's name, or an OCMD's (a membership dictionary) names joined with '+'."""
        o = obj.get_object()
        nm = o.get("/Name")
        if nm is not None:
            return str(nm)
        if o.get("/Type") == "/OCMD":
            g = o.get("/OCGs")
            if g is None:
                return None
            g = g.get_object()
            if isinstance(g, list):
                names = [str(x.get_object().get("/Name")) for x in g]
                return names[0] if len(names) == 1 else "+".join(names)
            return str(g.get("/Name"))
        return None

    def _props(self, res):
        out = {}
        p = res.get("/Properties")
        if p:
            for k, v in p.get_object().items():
                try:
                    out[k] = self._ocg_label(v)
                except Exception:                  # noqa: BLE001 - a malformed entry is no layer
                    out[k] = None
        return out

    def page(self, idx, flatten_tol=0.25):
        page = self.reader.pages[idx]
        res = page.get("/Resources")
        res = res.get_object() if res is not None else {}
        contents = page.get_contents()
        self.segs = []
        if contents is not None:
            self._run(contents.get_data(), res, (1, 0, 0, 1, 0, 0), [], 0, flatten_tol)
        return self.segs

    def _run(self, data, res, ctm, oc_stack, depth, tol):
        if depth > MAX_DEPTH:
            return
        pm = self._props(res)
        xo_res = res.get("/XObject")
        xo_res = xo_res.get_object() if xo_res else {}

        gs_stack = []
        cur = tuple(ctm)
        lw = 1.0
        stroke, fill = None, None
        dash = ()
        operands = []
        path = []          # finished subpaths, device-space points
        sub = None         # the current subpath
        cp = None          # the current point, user space (before the CTM)
        sp_start = None

        def dev(x, y):
            return xf(cur, x, y)

        pos = 0
        while True:
            m = TOK.search(data, pos)
            if m is None:
                break
            pos = m.end()
            g = m.lastgroup
            v = m.group()
            if g == 'num':
                operands.append(float(v))
                continue
            if g == 'name':
                operands.append(v.decode('latin1'))
                continue
            if g in ('str', 'hex'):
                operands.append(v)
                continue
            if g in ('arr_o', 'arr_c', 'dict_o', 'dict_c'):
                operands.append(v.decode())
                continue

            op = v.decode('latin1')
            nums = [x for x in operands if isinstance(x, float)]
            try:
                if op == 'ID':                     # skip an inline image's binary data
                    end = INLINE_END.search(data, pos)
                    pos = end.end() if end else len(data)
                elif op == 'q':
                    gs_stack.append((cur, lw, stroke, fill, dash))
                elif op == 'Q':
                    if gs_stack:
                        cur, lw, stroke, fill, dash = gs_stack.pop()
                elif op == 'cm' and len(operands) >= 6:
                    cur = mat_mul(tuple(operands[-6:]), cur)
                elif op == 'w' and operands:
                    lw = operands[-1]
                elif op == 'd':
                    # the dash array arrives as '[' n n ... ']' phase
                    try:
                        i = len(operands) - 1 - operands[::-1].index(']')
                        j = len(operands) - 1 - operands[::-1].index('[')
                        dash = tuple(round(float(x), 3) for x in operands[j + 1:i] if isinstance(x, float))
                    except ValueError:
                        dash = ()
                elif op in ('RG', 'G', 'K', 'SC', 'SCN'):
                    stroke = _colour(nums[-4:] if op == 'K' else nums) or stroke
                elif op in ('rg', 'g', 'k', 'sc', 'scn'):
                    fill = _colour(nums[-4:] if op == 'k' else nums) or fill
                elif op == 'BDC':
                    tag = operands[-2] if len(operands) >= 2 else None
                    prop = operands[-1] if operands else None
                    lab = None
                    if tag == '/OC' and isinstance(prop, str) and prop.startswith('/'):
                        lab = pm.get(prop)
                    oc_stack.append(lab if lab else (oc_stack[-1] if oc_stack else None))
                elif op == 'BMC':
                    oc_stack.append(oc_stack[-1] if oc_stack else None)
                elif op == 'EMC':
                    if oc_stack:
                        oc_stack.pop()

                # path construction: points transformed as they are appended
                elif op == 'm' and len(operands) >= 2:
                    cp = (operands[-2], operands[-1])
                    sp_start = cp
                    sub = [dev(*cp)]
                    path.append(sub)
                elif op == 'l' and len(operands) >= 2:
                    cp = (operands[-2], operands[-1])
                    if sub is None:
                        sub = []
                        path.append(sub)
                    sub.append(dev(*cp))
                elif op in ('c', 'v', 'y') and len(operands) >= (6 if op == 'c' else 4):
                    if op == 'c':
                        p1, p2, p3 = (operands[-6], operands[-5]), (operands[-4], operands[-3]), (operands[-2], operands[-1])
                    elif op == 'v':
                        p1, p2, p3 = None, (operands[-4], operands[-3]), (operands[-2], operands[-1])
                    else:
                        p1, p3 = (operands[-4], operands[-3]), (operands[-2], operands[-1])
                        p2 = p3
                    if sub is None or cp is None:
                        cp = p1 or p2
                        sub = [dev(*cp)]
                        path.append(sub)
                    p1 = p1 or cp
                    sub.extend(flatten_cubic(dev(*cp), dev(*p1), dev(*p2), dev(*p3), tol))
                    cp = p3
                elif op == 're' and len(operands) >= 4:
                    x, y, w, h = operands[-4:]
                    path.append([dev(x, y), dev(x + w, y), dev(x + w, y + h), dev(x, y + h), dev(x, y)])
                    sub = None
                    cp = (x, y)
                    sp_start = cp
                elif op == 'h':
                    if sub and len(sub) > 1:
                        sub.append(sub[0])
                    cp = sp_start

                elif op in PAINT_OPS:
                    lay = oc_stack[-1] if oc_stack else None
                    filled = op in FILL_OPS
                    colour = stroke if op in STROKE_OPS else fill
                    for s in path:
                        if len(s) >= 2:
                            self.segs.append({'layer': lay, 'op': op, 'pts': s, 'lw': lw * mat_scale(cur),
                                              'color': colour, 'fill': filled, 'dash': dash})
                    path = []
                    sub = None
                    cp = None

                elif op == 'Do' and operands:
                    xo = xo_res.get(operands[-1])
                    if xo is not None:
                        xo = xo.get_object()
                        if xo.get('/Subtype') == '/Form':
                            mtx = xo.get('/Matrix') or [1, 0, 0, 1, 0, 0]
                            sub_ctm = mat_mul(tuple(float(f) for f in mtx), cur)
                            sres = xo.get('/Resources')
                            sres = sres.get_object() if sres else res
                            # a form may itself sit on a layer (its /OC entry)
                            own = self._ocg_label(xo['/OC']) if xo.get('/OC') is not None else None
                            if own:
                                oc_stack.append(own)
                            self._run(xo.get_data(), sres, sub_ctm, oc_stack, depth + 1, tol)
                            if own:
                                oc_stack.pop()
            except Exception:                      # noqa: BLE001 - one malformed operator never stops a page
                pass
            operands = []


def extract(path, page_index, tol=0.25):
    """Every painted path on a page (module doc)."""
    return Extractor(path).page(page_index, tol)


def layer_names(path):
    """The layers (OCG names) the document declares, in its own order; [] for a PDF without layers."""
    r = pypdf.PdfReader(path)
    oc = r.trailer["/Root"].get("/OCProperties")
    if not oc:
        return []
    return [str(g.get_object().get("/Name")) for g in oc.get_object().get("/OCGs", [])]
