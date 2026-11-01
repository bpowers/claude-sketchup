load("lib/units.star", "sqrt", "total")

# The house.json element constructors (docs/IR.md).
#
# Each constructor returns a plain dict that is exactly one element of the document, after checking what the contract
# states: field types and ranges, an opening inside its wall, a unit's bars inside its opening. A constructor never
# repairs its input. A bad value fails the build with a message that names the element, so a mistake surfaces in the
# file where it was typed rather than as a quietly wrong house.
#
# Finishes are palette keys; the planner checks them against build/palette.json (Starlark cannot read files), so here a
# finish is only required to be a string.
#
# Coordinates are inches in the house's frame (lib/frame.star). Every z is absolute.

TYPES = ["space", "slab", "wall", "column", "beam", "stair", "box", "roof_plane"]

# Where an element's geometry comes from: a CAD drawing, a scan, a photo, the owner, a tape, or an assumption.
SRCS = ["cad", "scan", "photo", "owner", "measured", "assumed"]

WALL_KINDS = ["exterior", "interior", "half_wall", "glass_wall", "masonry"]
OPENING_KINDS = ["window", "door", "cased", "pass"]
UNIT_KINDS = ["window", "door", "cased"]           # a pass takes no unit
CASING_KINDS = ["window", "door", "cased"]
CASING_FACES = ["both", "left", "right"]
BOX_KINDS = ["base_cabinet", "upper_cabinet", "tall_cabinet", "counter", "appliance", "fixture", "soffit", "masonry",
             "hearth", "trim", "placeholder"]
BEAM_KINDS = ["structural", "faux"]
FRONTS = ["+x", "-x", "+y", "-y"]
SWING_HINGES = ["start", "end", "both"]
SWING_SIDES = ["left", "right"]
FRAME_SIDES = ["left", "right", "bottom", "top"]

# The plan directions a wall face may look toward.
FACE_DIRS = {"+x": [1.0, 0.0], "-x": [-1.0, 0.0], "+y": [0.0, 1.0], "-y": [0.0, -1.0]}

# A unit frame's member width where nobody measured one.
UNIT_FRAME = 2.0

# The narrowest light a unit may leave between its frame, mullions and rails. A light this thin is always an encoding
# mistake (a mullion typed on a jamb, a sash member laid over the frame), never a pane anyone measured.
MIN_LIGHT = 0.5

TOL = 0.01

# ------------------------------------------------------------------ scalars

def f2(v):
    """A number rounded to 1/100 for messages; this Starlark's % takes no precision ("%.2f" fails)."""
    n = int(v * 100.0 + (0.5 if v >= 0 else -0.5))
    return str(n / 100.0)

def _is_num(v):
    return type(v) == "int" or type(v) == "float"

def _num(v, what):
    if not _is_num(v):
        fail("%s: expected a number, got %r" % (what, v))
    return float(v)

def _bool(v, what):
    if type(v) != "bool":
        fail("%s: expected True or False, got %r" % (what, v))
    return v

def _xy(p, what):
    if (type(p) != "list" and type(p) != "tuple") or len(p) != 2:
        fail("%s: expected a point [x, y], got %r" % (what, p))
    return [_num(p[0], what), _num(p[1], what)]

def _xyz(p, what):
    if (type(p) != "list" and type(p) != "tuple") or len(p) != 3:
        fail("%s: expected a point [x, y, z], got %r" % (what, p))
    return [_num(p[0], what), _num(p[1], what), _num(p[2], what)]

def _strings(v, what):
    if type(v) != "list" or [s for s in v if type(s) != "string"]:
        fail("%s: expected a list of strings, got %r" % (what, v))
    return list(v)

def _finish(what, v):
    if type(v) != "string" or v == "":
        fail("%s: a finish is a palette key (build/palette.json), got %r" % (what, v))
    return v

def _token(v):
    """A lowercase token: letters, digits and _, starting with a letter."""
    if type(v) != "string" or v == "" or not v[0].isalpha():
        return False
    for c in v.elems():
        if not (c.isdigit() or c == "_" or (c.isalpha() and c.islower())):
            return False
    return True

def check_id(eid, what = "element"):
    """Ids are lowercase and dotted, with at least two tokens: kitchen.wall.back, living.wall.front.door."""
    if type(eid) != "string" or eid == "":
        fail("%s id must be a non-empty string, got %r" % (what, eid))
    parts = eid.split(".")
    if len(parts) < 2:
        fail("%s id %r is not dotted; ids look like zone.type.name" % (what, eid))
    for part in parts:
        if part == "":
            fail("%s id %r has an empty segment" % (what, eid))
        for c in part.elems():
            if not (c.isdigit() or c == "_" or (c.isalpha() and c.islower())):
                fail("%s id %r: segment %r may use only lowercase letters, digits and _" % (what, eid, part))
    return eid

def _common(eid, etype, zone, src, name, finish, tags, note):
    check_id(eid)
    if etype not in TYPES:
        fail("%s: unknown element type %r" % (eid, etype))
    if not _token(zone):
        fail("%s: zone %r is not a zone name (lowercase letters, digits and _); house.star declares the zones" % (eid, zone))
    if src == None:
        fail("%s: give its src, where its geometry comes from: one of %s" % (eid, SRCS))
    if src not in SRCS:
        fail("%s: unknown src %r; the contract allows %s" % (eid, src, SRCS))
    d = {"id": eid, "type": etype, "zone": zone, "src": src}
    if name != None:
        d["name"] = name
    if finish != None:
        d["finish"] = _finish(eid, finish)
    if tags:
        d["tags"] = _strings(tags, eid + " tags")
    if note != None:
        d["note"] = note
    return d

# ------------------------------------------------------------------ zones

def zone(storey, outdoor = False):
    """A zone's declaration for house.star's "zones" table. `storey` orders zones for render visibility: a camera
    standing in a space of storey k hides every zone of a higher storey (the roof is just a high storey). An `outdoor`
    zone's spaces count as outside: a camera standing in one hides nothing."""
    if type(storey) != "int":
        fail("zone: storey must be an integer, got %r" % (storey,))
    z = {"storey": storey}
    if _bool(outdoor, "zone outdoor"):
        z["outdoor"] = True
    return z

def check_zones(zones, elements):
    """Every element's zone is declared in `zones` (house.star's table of zone(...) entries); returns the zones used."""
    used = {}
    for e in elements:
        if e["zone"] not in zones:
            fail("%s: zone %r is not declared in house.star's zones (%s)" % (e["id"], e["zone"], ", ".join(sorted(zones))))
        used[e["zone"]] = True
    return sorted(used)

# ------------------------------------------------------------------ polygons

def polygon_area(pts):
    """Signed area; positive for counter-clockwise."""
    a = 0.0
    n = len(pts)
    for i in range(n):
        p = pts[i]
        q = pts[(i + 1) % n]
        a += p[0] * q[1] - q[0] * p[1]
    return a / 2.0

def _orient(p, q, r):
    v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    if v > 1e-9:
        return 1
    if v < -1e-9:
        return -1
    return 0

def _on_segment(p, q, r):
    """Is q on segment p-r, given the three are collinear?"""
    return (min(p[0], r[0]) - 1e-9 <= q[0] and q[0] <= max(p[0], r[0]) + 1e-9 and
            min(p[1], r[1]) - 1e-9 <= q[1] and q[1] <= max(p[1], r[1]) + 1e-9)

def _segments_touch(p1, p2, p3, p4):
    o1 = _orient(p1, p2, p3)
    o2 = _orient(p1, p2, p4)
    o3 = _orient(p3, p4, p1)
    o4 = _orient(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and _on_segment(p1, p3, p2)) or (o2 == 0 and _on_segment(p1, p4, p2)) or
            (o3 == 0 and _on_segment(p3, p1, p4)) or (o4 == 0 and _on_segment(p3, p2, p4)))

def check_polygon(eid, outline):
    """A simple counter-clockwise polygon, not closed (no repeated first point)."""
    if type(outline) != "list" or len(outline) < 3:
        fail("%s: an outline needs at least three points, got %r" % (eid, outline))
    pts = [_xy(p, eid + " outline") for p in outline]
    n = len(pts)
    for i in range(n):
        q = pts[(i + 1) % n]
        if abs(pts[i][0] - q[0]) < TOL and abs(pts[i][1] - q[1]) < TOL:
            fail("%s: outline vertex %d repeats the next one; do not close the ring" % (eid, i))
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _segments_touch(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                fail("%s: the outline is not simple; edge %d touches edge %d" % (eid, i, j))
    area = polygon_area(pts)
    if area <= 0.0:
        fail("%s: the outline must run counter-clockwise; its signed area is %s" % (eid, f2(area)))
    return pts

def rect(x0, y0, x1, y1):
    """A counter-clockwise axis-aligned rectangle."""
    if not (x0 < x1 and y0 < y1):
        fail("rect: need x0 < x1 and y0 < y1, got [%s, %s, %s, %s]" % (f2(x0), f2(y0), f2(x1), f2(y1)))
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

def point_in_polygon(pts, x, y):
    inside = False
    n = len(pts)
    for i in range(n):
        p = pts[i]
        q = pts[(i + 1) % n]
        if (p[1] > y) != (q[1] > y):
            xi = p[0] + (y - p[1]) * (q[0] - p[0]) / (q[1] - p[1])
            if x < xi:
                inside = not inside
    return inside

def _axis_values(vals):
    """Sorted distinct values, a value within 0.001 of the one before it taken as that one."""
    out = []
    for v in sorted(vals):
        if len(out) == 0 or v - out[-1] > 0.001:
            out.append(v)
    return out

def ortho_union(eid, polys, cuts = []):
    """The counter-clockwise outline of the union of plan polygons whose edges all run along x or y, less the polygons
    `cuts`: an L-shaped room from two rectangles. The result must be one piece, with no hole and no two parts meeting
    only at a corner."""
    for P in polys + cuts:
        for i in range(len(P)):
            p = P[i]
            q = P[(i + 1) % len(P)]
            if abs(p[0] - q[0]) > 0.001 and abs(p[1] - q[1]) > 0.001:
                fail("%s: ortho_union takes polygons whose edges run along x or y, got the edge %r -> %r" % (eid, p, q))
    xs = _axis_values([p[0] for P in polys + cuts for p in P])
    ys = _axis_values([p[1] for P in polys + cuts for p in P])

    # the cells of the grid on every vertex coordinate whose centres lie in one of polys and in none of cuts
    inside = {}
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx = (xs[i] + xs[i + 1]) / 2.0
            cy = (ys[j] + ys[j + 1]) / 2.0
            if [P for P in polys if point_in_polygon(P, cx, cy)] and not [P for P in cuts if point_in_polygon(P, cx, cy)]:
                inside[(i, j)] = True

    # each cell side between an inside and an outside cell, directed with the inside on its left
    nxt = {}
    for key in inside:
        i = key[0]
        j = key[1]
        for other, a, b in [((i, j - 1), (i, j), (i + 1, j)), ((i + 1, j), (i + 1, j), (i + 1, j + 1)),
                            ((i, j + 1), (i + 1, j + 1), (i, j + 1)), ((i - 1, j), (i, j + 1), (i, j))]:
            if other in inside:
                continue
            if a in nxt:
                fail("%s: the union's parts meet only at a corner, at (%s, %s)" % (eid, f2(xs[a[0]]), f2(ys[a[1]])))
            nxt[a] = b
    if len(nxt) == 0:
        fail("%s: the union is empty" % eid)
    start = sorted(nxt.keys())[0]
    ring = [start]
    k = nxt[start]
    for _ in range(len(nxt)):
        if k == start:
            break
        ring.append(k)
        k = nxt[k]
    if len(ring) != len(nxt):
        fail("%s: the union is not one piece without holes" % eid)

    # drop the corners where the boundary runs straight on
    n = len(ring)
    out = []
    for m in range(n):
        p = ring[(m + n - 1) % n]
        q = ring[m]
        r = ring[(m + 1) % n]
        if (p[0] == q[0] and q[0] == r[0]) or (p[1] == q[1] and q[1] == r[1]):
            continue
        out.append([xs[q[0]], ys[q[1]]])
    return out

# ------------------------------------------------------------------ spaces and slabs

def ceiling_plane_z(plane, x, y):
    """The height of a sloped ceiling ({"at": [x, y, z], "slope": [dz/dx, dz/dy]}) over plan point (x, y)."""
    return plane["at"][2] + plane["slope"][0] * (x - plane["at"][0]) + plane["slope"][1] * (y - plane["at"][1])

def space(eid, zone, outline, floor_z, ceiling_z = None, ceiling_plane = None, floor_finish = None,
          ceiling_finish = None, build_floor = True, build_ceiling = None, src = None, name = None, tags = [],
          note = None):
    """A room or outdoor area: a floor face at floor_z and optionally a ceiling face, flat at ceiling_z or sloped on
    ceiling_plane. build_ceiling defaults to True when a ceiling is given. An outdoor area whose floor another element
    draws (a porch slab) is a space with build_floor = False: it still tells a camera where outside is."""
    d = _common(eid, "space", zone, src, name, None, tags, note)
    pts = check_polygon(eid, outline)
    fz = _num(floor_z, eid + " floor_z")
    if ceiling_z != None and ceiling_plane != None:
        fail("%s: give one of ceiling_z and ceiling_plane, not both" % eid)
    has_ceiling = ceiling_z != None or ceiling_plane != None
    if build_ceiling == None:
        build_ceiling = has_ceiling
    elif _bool(build_ceiling, eid + " build_ceiling") and not has_ceiling:
        fail("%s: build_ceiling needs a ceiling_z or a ceiling_plane" % eid)
    d["outline"] = pts
    d["floor_z"] = fz
    if ceiling_z != None:
        cz = _num(ceiling_z, eid + " ceiling_z")
        if cz <= fz:
            fail("%s: ceiling_z %s must be above floor_z %s" % (eid, f2(cz), f2(fz)))
        d["ceiling_z"] = cz
    if ceiling_plane != None:
        if type(ceiling_plane) != "dict" or sorted(ceiling_plane.keys()) != ["at", "slope"]:
            fail("%s: ceiling_plane must be {\"at\": [x, y, z], \"slope\": [dz/dx, dz/dy]}, got %r" % (eid, ceiling_plane))
        plane = {"at": _xyz(ceiling_plane["at"], eid + " ceiling_plane.at"),
                 "slope": _xy(ceiling_plane["slope"], eid + " ceiling_plane.slope")}
        for q in pts:
            if ceiling_plane_z(plane, q[0], q[1]) <= fz:
                fail("%s: ceiling_plane is not above floor_z over outline corner (%s, %s)" % (eid, f2(q[0]), f2(q[1])))
        d["ceiling_plane"] = plane
    if floor_finish != None:
        d["floor_finish"] = _finish(eid + " floor_finish", floor_finish)
    if ceiling_finish != None:
        d["ceiling_finish"] = _finish(eid + " ceiling_finish", ceiling_finish)
    d["build_floor"] = _bool(build_floor, eid + " build_floor")
    if has_ceiling:
        d["build_ceiling"] = build_ceiling
    return d

def slab(eid, zone, outline, z_top, thickness, src = None, name = None, finish = None, tags = [], note = None):
    """A horizontal prism from z_top down by thickness: a porch slab, a floor structure, a soffit."""
    d = _common(eid, "slab", zone, src, name, finish, tags, note)
    d["outline"] = check_polygon(eid, outline)
    d["z_top"] = _num(z_top, eid + " z_top")
    t = _num(thickness, eid + " thickness")
    if t <= 0.0:
        fail("%s: slab thickness must be positive, got %s" % (eid, f2(t)))
    d["thickness"] = t
    return d

# ------------------------------------------------------------------ openings

def swing(hinge, side):
    """A door's swing, recorded and not drawn: `hinge` is the jamb the leaf hangs on, "start" (at the opening's `at`),
    "end" (at `at + width`) or "both" for a pair; `side` is the face of the wall it opens toward, "left" or "right" of
    the wall's a -> b (left is a -> b turned +90 degrees)."""
    if hinge not in SWING_HINGES:
        fail("swing: hinge must be one of %s, got %r" % (SWING_HINGES, hinge))
    if side not in SWING_SIDES:
        fail("swing: side must be one of %s, got %r" % (SWING_SIDES, side))
    return {"hinge": hinge, "side": side}

def swing_side(a, b, point):
    """"left" or "right" of the wall a -> b for a plan point, such as a point in the room a leaf opens into."""
    cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
    if abs(cross) < 1e-9:
        fail("swing_side: point %r lies on the wall line %r -> %r" % (point, a, b))
    return "left" if cross > 0 else "right"

def along(a, b, point):
    """The distance from a along the line a -> b to where a plan point falls square onto it: an opening's `at` from a
    point the drawing gives, such as a jamb's station on a dimension string, along(a, b, [x, JAMB_Y])."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    return ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / sqrt(dx * dx + dy * dy)

def hinge_jamb(a, b, at, width, point):
    """"start" or "end": the jamb of an opening nearer a plan point, such as the hinge a drawing marks."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    s = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / sqrt(dx * dx + dy * dy)
    return "start" if abs(s - at) <= abs(s - (at + width)) else "end"

def _frame_value(what, frame):
    """unit.frame as a number, or as an object naming exactly the four sides."""
    if type(frame) == "dict":
        if sorted(frame.keys()) != sorted(FRAME_SIDES):
            fail("%s: unit frame as an object names exactly %s, got %r" % (what, FRAME_SIDES, sorted(frame.keys())))
        out = {}
        for k in FRAME_SIDES:
            out[k] = _num(frame[k], "%s unit frame.%s" % (what, k))
            if out[k] < 0.0:
                fail("%s: unit frame.%s is %s; a frame side must not be negative" % (what, k, f2(out[k])))
        return out
    v = _num(frame, what + " unit frame")
    if v < 0.0:
        fail("%s: unit frame is %s; it must not be negative" % (what, f2(v)))
    return v

def frame_sides(frame, kind):
    """The four frame widths of a unit: the number form frames a window on four sides and a door or cased opening on
    three, with no member across the threshold; the object form names every side for every kind."""
    if type(frame) == "dict":
        return {k: frame[k] for k in FRAME_SIDES}
    return {"left": frame, "right": frame, "bottom": frame if kind == "window" else 0.0, "top": frame}

def unit(frame = UNIT_FRAME, verticals = [], mullion = 0.0, horizontals = [], rail = 0.0, glazed = True, finish = None,
         leaf_finish = None, depth = None, offset = None):
    """What fills an opening: a frame, mullions (`verticals`, centres measured from the opening's start, `mullion`
    wide), rails (`horizontals`, absolute z of their centres, `rail` wide), and in each light glass (`glazed`) or, for an
    unglazed door, a leaf panel. `frame` is a number or {"left", "right", "bottom", "top"}. `finish` paints the frame,
    mullions and rails, `leaf_finish` the leaf panels. `depth` is the frame's depth across the wall and `offset` its
    centre's distance from the wall's centreline toward the left face. opening() checks it against the opening."""
    u = {"frame": _frame_value("unit", frame), "verticals": [_num(v, "unit verticals") for v in verticals],
         "mullion": _num(mullion, "unit mullion"), "horizontals": [_num(h, "unit horizontals") for h in horizontals],
         "rail": _num(rail, "unit rail"), "glazed": _bool(glazed, "unit glazed")}
    if finish != None:
        u["finish"] = _finish("unit finish", finish)
    if leaf_finish != None:
        u["leaf_finish"] = _finish("unit leaf_finish", leaf_finish)
    if depth != None:
        u["depth"] = _num(depth, "unit depth")
    if offset != None:
        u["offset"] = _num(offset, "unit offset")
    return u

def glazed_unit(frame = UNIT_FRAME, verticals = [], mullion = 0.0, horizontals = [], rail = 0.0, finish = None,
                depth = None, offset = None):
    """A unit with glass in every light: a window, a glazed door."""
    return unit(frame, verticals, mullion, horizontals, rail, True, finish, None, depth, offset)

def leaf_unit(frame = 0.75, verticals = [], mullion = 0.0, horizontals = [], rail = 0.0, finish = None,
              leaf_finish = None, depth = None, offset = None):
    """A unit with a solid leaf panel in every light: a panel door. The default frame is a thin jamb lining."""
    return unit(frame, verticals, mullion, horizontals, rail, False, finish, leaf_finish, depth, offset)

def split_lights(lo, hi, lights, bar):
    """The centres of the bars, `bar` wide, that split the run lo..hi into `lights` equal lights: a window's mullions
    are split_lights(frame, width - frame, 3, mullion), its rails split_lights(sill + frame, head - frame, 2, rail)."""
    if type(lights) != "int" or lights < 1:
        fail("split_lights: lights must be a positive integer, got %r" % (lights,))
    glass = (hi - lo - bar * (lights - 1)) / lights
    if glass < MIN_LIGHT:
        fail("split_lights: %d lights leave %s of glass each in %s..%s" % (lights, f2(glass), f2(lo), f2(hi)))
    return [lo + (k + 1) * glass + k * bar + bar / 2.0 for k in range(lights - 1)]

def _unit_bars(oid, what, lo, hi, centres, width):
    """Bars `width` wide centred on increasing positions split the light lo..hi; each leaves a light before it, and the
    last one a light before the frame."""
    cur = lo
    for c in centres:
        if c - width / 2.0 - cur < MIN_LIGHT:
            fail("%s: the unit's %s %s leaves no light before it (the light starts at %s). Every mullion and rail " % (
                oid, what, f2(c), f2(cur)) + "leaves a light on both sides inside the frame; a deep member at the " +
                 "frame is the frame's own width: give unit frame as {\"left\", \"right\", \"bottom\", \"top\"}.")
        cur = c + width / 2.0
    if centres and hi - cur < MIN_LIGHT:
        fail("%s: the unit's last %s leaves no light before the frame (it ends at %s, the light at %s)" % (
            oid, what, f2(cur), f2(hi)))

def _check_unit(oid, o, u):
    """A unit as opening() takes it: known fields, bars inside the opening with a width when there are any, a clear
    light inside the frame, and a light on both sides of every bar."""
    if o["kind"] not in UNIT_KINDS:
        fail("%s: a %s opening takes no unit" % (oid, o["kind"]))
    if type(u) != "dict":
        fail("%s: unit must be a unit(...), got %r" % (oid, u))
    for key in u.keys():
        if key not in ["frame", "verticals", "mullion", "horizontals", "rail", "glazed", "finish", "leaf_finish",
                       "depth", "offset"]:
            fail("%s: the unit has an unknown field %r" % (oid, key))
    vs = u.get("verticals", [])
    hs = u.get("horizontals", [])
    for i in range(1, len(vs)):
        if not vs[i] > vs[i - 1]:
            fail("%s: unit verticals %r must increase" % (oid, vs))
    for i in range(1, len(hs)):
        if not hs[i] > hs[i - 1]:
            fail("%s: unit horizontals %r must increase" % (oid, hs))
    if vs and not u.get("mullion", 0.0) > 0.0:
        fail("%s: unit verticals need a mullion width greater than 0" % oid)
    if hs and not u.get("rail", 0.0) > 0.0:
        fail("%s: unit horizontals need a rail width greater than 0" % oid)
    sides = frame_sides(_frame_value(oid, u.get("frame", UNIT_FRAME)), o["kind"])
    d_lo = sides["left"]
    d_hi = o["width"] - sides["right"]
    z_lo = o["sill_z"] + sides["bottom"]
    z_hi = o["head_z"] - sides["top"]
    if d_hi - d_lo < MIN_LIGHT or z_hi - z_lo < MIN_LIGHT:
        fail("%s: the unit's frame %r leaves no clear light in the %s x %s opening" % (
            oid, sides, f2(o["width"]), f2(o["head_z"] - o["sill_z"])))
    _unit_bars(oid, "mullion at", d_lo, d_hi, vs, u.get("mullion", 0.0))
    _unit_bars(oid, "rail at z", z_lo, z_hi, hs, u.get("rail", 0.0))
    return u

def casing(width = 3.5, proud = 0.75, finish = None, faces = "both"):
    """The trim boards round an opening, standing `proud` off the wall's faces. `width` is a number, the boards on three
    sides of a door or cased opening and four of a window, or {"left", "right", "bottom", "top"} (left at the opening's
    start), each 0 for no board on that side or more: an opening that runs to its wall's end has no room for a board
    there. `faces` is "both", "left" or "right" of the wall's a -> b, or the plan direction the face looks toward
    ("+x", "-x", "+y", "-y"), which wall() turns into left or right."""
    if type(width) == "dict":
        if sorted(width.keys()) != sorted(FRAME_SIDES):
            fail("casing: width as an object names exactly %s, got %r" % (FRAME_SIDES, sorted(width.keys())))
        w = {}
        for k in FRAME_SIDES:
            w[k] = _num(width[k], "casing width.%s" % k)
            if w[k] < 0.0:
                fail("casing: width.%s is %s; a side is 0 (no board) or more" % (k, f2(w[k])))
        if not [k for k in FRAME_SIDES if w[k] > 0.0]:
            fail("casing: every side of width is 0, so there is no casing")
    else:
        w = _num(width, "casing width")
        if w <= 0.0:
            fail("casing: width %s must be positive" % f2(w))
    p = _num(proud, "casing proud")
    if p <= 0.0:
        fail("casing: proud %s must be positive" % f2(p))
    if faces not in CASING_FACES and faces not in FACE_DIRS:
        fail("casing: faces %r is not one of %s or a plan direction %s" % (faces, CASING_FACES, sorted(FACE_DIRS)))
    c = {"width": w, "proud": p, "faces": faces}
    if finish != None:
        c["finish"] = _finish("casing finish", finish)
    return c

def opening(oid, kind, at, width, sill_z, head_z, swing = None, unit = None, casing = None, src = None, note = None):
    """An opening in a wall: `at` runs from the wall's a end along a -> b to the opening's start; sill_z and head_z are
    absolute. A door may carry a swing(...); a window, door or cased opening a unit(...) and a casing(...). wall()
    checks that it fits in its wall."""
    check_id(oid, "opening")
    if kind not in OPENING_KINDS:
        fail("%s: unknown opening kind %r; the contract allows %s" % (oid, kind, OPENING_KINDS))
    o = {"id": oid, "kind": kind, "at": _num(at, oid + " at"), "width": _num(width, oid + " width"),
         "sill_z": _num(sill_z, oid + " sill_z"), "head_z": _num(head_z, oid + " head_z")}
    if o["width"] <= 0.0:
        fail("%s: opening width must be positive, got %s" % (oid, f2(o["width"])))
    if not o["sill_z"] < o["head_z"]:
        fail("%s: sill_z %s must be below head_z %s" % (oid, f2(o["sill_z"]), f2(o["head_z"])))
    if swing != None:
        if kind != "door":
            fail("%s: only doors swing; this opening is a %s" % (oid, kind))
        if type(swing) != "dict" or sorted(swing.keys()) != ["hinge", "side"]:
            fail("%s: swing must be a swing(hinge, side), got %r" % (oid, swing))
        o["swing"] = swing
    if unit != None:
        o["unit"] = _check_unit(oid, o, unit)
    if casing != None:
        if kind not in CASING_KINDS:
            fail("%s: a %s opening takes no casing" % (oid, kind))
        if type(casing) != "dict" or "width" not in casing:
            fail("%s: casing must be a casing(...), got %r" % (oid, casing))
        o["casing"] = casing
    if src != None:
        if src not in SRCS:
            fail("%s: unknown src %r; the contract allows %s" % (oid, src, SRCS))
        o["src"] = src
    if note != None:
        o["note"] = note
    return o

# ------------------------------------------------------------------ walls

def wall_length(w):
    dx = w["b"][0] - w["a"][0]
    dy = w["b"][1] - w["a"][1]
    return sqrt(dx * dx + dy * dy)

def wall_top_at(w, s):
    """A wall's top at distance s from a: straight from top_a to top_b."""
    return w["top_a"] + (w["top_b"] - w["top_a"]) * s / wall_length(w)

def _face_of(eid, a, b, direction):
    """"left" or "right": the face of the wall a -> b that looks toward a plan direction."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    d = FACE_DIRS[direction]
    cos = (d[0] * -dy + d[1] * dx) / sqrt(dx * dx + dy * dy)
    if cos > 0.999:
        return "left"
    if cos < -0.999:
        return "right"
    fail("%s: no face of the wall %r -> %r looks toward %s" % (eid, a, b, direction))

def face_finishes(eid, a, b, faces):
    """[finish_left, finish_right] of the wall a -> b from finishes keyed by the direction each face looks toward:
    {"-y": "siding", "+y": "drywall"}. Left and right swap when a wall is drawn the other way; a face named by where it
    looks cannot, so wall(faces = ...) resolves the names once."""
    if type(faces) != "dict" or len(faces) != 2:
        fail("%s: faces names the wall's two faces by direction, e.g. {\"-y\": \"siding\", \"+y\": \"drywall\"}, got %r" % (
            eid, faces))
    out = {}
    for key in faces.keys():
        if key not in FACE_DIRS:
            fail("%s: face direction %r is not one of %s" % (eid, key, sorted(FACE_DIRS)))
        out[_face_of(eid, a, b, key)] = _finish(eid + " faces", faces[key])
    if len(out) != 2:
        fail("%s: faces %r names one face twice and the other not at all" % (eid, faces))
    return [out["left"], out["right"]]

def wall(eid, zone, a, b, thickness, z0, top_a, top_b = None, kind = "interior", openings = [], faces = None,
         finish = None, finish_left = None, finish_right = None, src = None, name = None, tags = [], note = None):
    """A wall by its centreline a -> b, thickness/2 either side, from z0 up to top_a at a and top_b at b (top_a without
    one). Walls meeting at a corner both run to the other's outer face; a wall meeting another at a T stops at its
    near face. `faces` gives the two faces' finishes by the direction each looks toward and becomes finish_left and
    finish_right; `finish` paints the rest (ends, top, reveals). Openings are opening(...)s, kept in order along it."""
    d = _common(eid, "wall", zone, src, name, finish, tags, note)
    pa = _xy(a, eid + " a")
    pb = _xy(b, eid + " b")
    t = _num(thickness, eid + " thickness")
    if t <= 0.0:
        fail("%s: wall thickness must be positive, got %s" % (eid, f2(t)))
    base = _num(z0, eid + " z0")
    ta = _num(top_a, eid + " top_a")
    tb = ta if top_b == None else _num(top_b, eid + " top_b")
    if not (base < ta and base < tb):
        fail("%s: z0 %s must be below both tops (%s, %s)" % (eid, f2(base), f2(ta), f2(tb)))
    if kind not in WALL_KINDS:
        fail("%s: unknown wall kind %r; the contract allows %s" % (eid, kind, WALL_KINDS))
    d.update({"a": pa, "b": pb, "thickness": t, "z0": base, "top_a": ta, "top_b": tb, "kind": kind})
    length = wall_length(d)
    if length < 0.5:
        fail("%s: the wall is %s long; a and b nearly coincide" % (eid, f2(length)))
    if faces != None:
        if finish_left != None or finish_right != None:
            fail("%s: give faces or finish_left and finish_right, not both" % eid)
        finish_left, finish_right = face_finishes(eid, pa, pb, faces)
    if finish_left != None:
        d["finish_left"] = _finish(eid + " finish_left", finish_left)
    if finish_right != None:
        d["finish_right"] = _finish(eid + " finish_right", finish_right)
    ops = []
    end = None
    for o in sorted(openings, key = lambda o: o["at"]):
        if o["at"] < -TOL or o["at"] + o["width"] > length + TOL:
            fail("%s: opening %s at %s, %s wide, falls outside the %s long wall" % (
                eid, o["id"], f2(o["at"]), f2(o["width"]), f2(length)))
        if end != None and o["at"] < end - TOL:
            fail("%s: opening %s at %s overlaps the opening before it, which ends at %s" % (
                eid, o["id"], f2(o["at"]), f2(end)))
        end = o["at"] + o["width"]
        top = min(wall_top_at(d, o["at"]), wall_top_at(d, end))
        if o["head_z"] > top + TOL:
            fail("%s: opening %s's head %s is above the wall's top over it, %s" % (eid, o["id"], f2(o["head_z"]), f2(top)))
        if o["sill_z"] < base - TOL:
            fail("%s: opening %s's sill %s is below the wall's base %s" % (eid, o["id"], f2(o["sill_z"]), f2(base)))
        if "unit" in o:
            u = o["unit"]
            depth = u.get("depth", min(t, 3.5))
            if not (0.0 < depth and depth <= t + TOL) or abs(u.get("offset", 0.0)) > (t - depth) / 2.0 + TOL:
                fail("%s: the unit of %s, %s deep at offset %s, does not stay inside the %s thick wall" % (
                    eid, o["id"], f2(depth), f2(u.get("offset", 0.0)), f2(t)))
        if "casing" in o and o["casing"]["faces"] in FACE_DIRS:
            o = dict(o, casing = dict(o["casing"], faces = _face_of(eid, pa, pb, o["casing"]["faces"])))
        ops.append(o)
    d["openings"] = ops
    return d

# ------------------------------------------------------------------ frame members

def column(eid, zone, center, z0, z1, shape = "square", size = None, diameter = None, capital = None,
           src = None, name = None, finish = None, tags = [], note = None):
    """A post: square (`size` [w, d]) or round (`diameter`), from z0 to z1, with an optional capital
    {"size": [w, d], "height"} occupying its top."""
    d = _common(eid, "column", zone, src, name, finish, tags, note)
    d["center"] = _xy(center, eid + " center")
    d["shape"] = shape
    if shape == "square":
        if size == None or diameter != None:
            fail("%s: a square column takes size = [w, d] and no diameter" % eid)
        s = _xy(size, eid + " size")
        if s[0] <= 0.0 or s[1] <= 0.0:
            fail("%s: column size must be positive, got %r" % (eid, s))
        d["size"] = s
        least = min(s[0], s[1])
    elif shape == "round":
        if diameter == None or size != None:
            fail("%s: a round column takes a diameter and no size" % eid)
        least = _num(diameter, eid + " diameter")
        if least <= 0.0:
            fail("%s: column diameter must be positive" % eid)
        d["diameter"] = least
    else:
        fail("%s: column shape must be square or round, got %r" % (eid, shape))
    d["z0"] = _num(z0, eid + " z0")
    d["z1"] = _num(z1, eid + " z1")
    if d["z0"] >= d["z1"]:
        fail("%s: column z0 %s must be below z1 %s" % (eid, f2(d["z0"]), f2(d["z1"])))
    if capital != None:
        if type(capital) != "dict" or sorted(capital.keys()) != ["height", "size"]:
            fail("%s: capital must be {\"size\": [w, d], \"height\": h}, got %r" % (eid, capital))
        cs = _xy(capital["size"], eid + " capital size")
        ch = _num(capital["height"], eid + " capital height")
        if ch <= 0.0 or ch >= d["z1"] - d["z0"]:
            fail("%s: capital height %s must lie inside the column's height" % (eid, f2(ch)))
        if min(cs[0], cs[1]) < least:
            fail("%s: capital %r is smaller than the column it caps" % (eid, cs))
        d["capital"] = {"size": cs, "height": ch}
    return d

def beam(eid, zone, a, b, width, depth, soffit_z, kind = "structural", src = None, name = None, finish = None,
         tags = [], note = None):
    """A box width/2 either side of a -> b, from soffit_z up by depth."""
    d = _common(eid, "beam", zone, src, name, finish, tags, note)
    pa = _xy(a, eid + " a")
    pb = _xy(b, eid + " b")
    if sqrt((pb[0] - pa[0]) * (pb[0] - pa[0]) + (pb[1] - pa[1]) * (pb[1] - pa[1])) < 0.5:
        fail("%s: beam a and b nearly coincide" % eid)
    w = _num(width, eid + " width")
    dep = _num(depth, eid + " depth")
    if w <= 0.0 or dep <= 0.0:
        fail("%s: beam width and depth must be positive" % eid)
    if kind not in BEAM_KINDS:
        fail("%s: unknown beam kind %r; the contract allows %s" % (eid, kind, BEAM_KINDS))
    d.update({"a": pa, "b": pb, "width": w, "depth": dep, "soffit_z": _num(soffit_z, eid + " soffit_z"), "kind": kind})
    return d

def stair(eid, zone, a, b, descend, z_top, risers, rise, run, src = None, name = None, finish = None, tags = [],
          note = None):
    """A straight run: a -> b is the top nosing line, `descend` the unit vector square to it pointing toward the lower
    floor; `risers` risers of `rise` each, treads `run` deep."""
    d = _common(eid, "stair", zone, src, name, finish, tags, note)
    pa = _xy(a, eid + " a")
    pb = _xy(b, eid + " b")
    dv = _xy(descend, eid + " descend")
    dx = pb[0] - pa[0]
    dy = pb[1] - pa[1]
    length = sqrt(dx * dx + dy * dy)
    if length < 0.5:
        fail("%s: the stair's nosing line is too short" % eid)
    if abs(sqrt(dv[0] * dv[0] + dv[1] * dv[1]) - 1.0) > 1e-6:
        fail("%s: descend %r must be a unit vector" % (eid, dv))
    if abs((dx * dv[0] + dy * dv[1]) / length) > 1e-6:
        fail("%s: descend %r must be square to the nosing line a -> b" % (eid, dv))
    if type(risers) != "int" or risers < 1:
        fail("%s: risers must be a positive integer, got %r" % (eid, risers))
    r = _num(rise, eid + " rise")
    n = _num(run, eid + " run")
    if r <= 0.0 or n <= 0.0:
        fail("%s: rise and run must be positive" % eid)
    d.update({"a": pa, "b": pb, "descend": dv, "z_top": _num(z_top, eid + " z_top"), "risers": risers, "rise": r,
              "run": n})
    return d

def stair_bottom_z(s):
    return s["z_top"] - s["risers"] * s["rise"]

def box(eid, zone, bbox, kind, subtype = None, front = None, seams = [], rails = [], src = None, name = None,
        finish = None, tags = [], note = None):
    """An axis-aligned box [x0, y0, z0, x1, y1, z1]: casework, an appliance, a fixture, a soffit, trim, a placeholder.
    `front` is the direction its front faces; `seams` (positions along the front from its low end) and `rails`
    (absolute z) draw door and drawer lines on the front."""
    d = _common(eid, "box", zone, src, name, finish, tags, note)
    if type(bbox) != "list" or len(bbox) != 6:
        fail("%s: bbox must be [x0, y0, z0, x1, y1, z1], got %r" % (eid, bbox))
    bb = [_num(v, eid + " bbox") for v in bbox]
    for i in range(3):
        if not bb[i] < bb[i + 3]:
            fail("%s: bbox minima must be below maxima, got %r" % (eid, bb))
    if kind not in BOX_KINDS:
        fail("%s: unknown box kind %r; the contract allows %s" % (eid, kind, BOX_KINDS))
    d["bbox"] = bb
    d["kind"] = kind
    if subtype != None:
        d["subtype"] = subtype
    if front != None:
        if front not in FRONTS:
            fail("%s: front must be one of %s, got %r" % (eid, FRONTS, front))
        d["front"] = front
        span = (bb[4] - bb[1]) if front in ["+x", "-x"] else (bb[3] - bb[0])
        for s in seams:
            if not (0.0 < s and s < span):
                fail("%s: a seam at %s lies outside the %s wide front" % (eid, f2(s), f2(span)))
        d["seams"] = [float(s) for s in seams]
    elif seams:
        fail("%s: seams need a front" % eid)
    for r in rails:
        if not (bb[2] < r and r < bb[5]):
            fail("%s: a rail at z %s lies outside the box" % (eid, f2(r)))
    if rails:
        d["rails"] = [float(r) for r in rails]
    return d

def door_seams(length, widest):
    """The seams that divide a cabinet front `length` long into the fewest equal doors no wider than `widest`, as box()
    takes them: positions from the front's low end."""
    if not (length > 0.0 and widest > 0.0):
        fail("door_seams: length %s and widest %s must be positive" % (f2(length), f2(widest)))
    n = int(length / widest)
    if n * widest < length - 0.001:
        n += 1
    return [length * k / n for k in range(1, n)]

def roof_plane(eid, zone, pts, thickness, holes = None, src = None, name = None, finish = None, tags = [],
               note = None):
    """A planar polygon `pts` [[x, y, z], ...], the roof's top face, swept vertically down by thickness; `holes` are plan
    rectangles [x0, y0, x1, y1] cut through it (skylights)."""
    d = _common(eid, "roof_plane", zone, src, name, finish, tags, note)
    if type(pts) != "list" or len(pts) < 3:
        fail("%s: a roof plane needs at least three points" % eid)
    P = [_xyz(p, eid + " pts") for p in pts]
    check_polygon(eid, [[p[0], p[1]] for p in P])
    # the best plane through the points, as the planner fits it: Newell's normal through their centroid
    n = [0.0, 0.0, 0.0]
    for i in range(len(P)):
        a = P[i]
        b = P[(i + 1) % len(P)]
        n = [n[0] + (a[1] - b[1]) * (a[2] + b[2]), n[1] + (a[2] - b[2]) * (a[0] + b[0]),
             n[2] + (a[0] - b[0]) * (a[1] + b[1])]
    mag = sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
    if mag < 1e-6:
        fail("%s: the roof points describe a degenerate polygon" % eid)
    c = [total([p[i] for p in P]) / len(P) for i in range(3)]
    for p in P:
        dist = ((p[0] - c[0]) * n[0] + (p[1] - c[1]) * n[1] + (p[2] - c[2]) * n[2]) / mag
        if abs(dist) > TOL:
            fail("%s: point %r is %s in off the roof plane's best plane (more than %s)" % (eid, p, f2(dist), f2(TOL)))
    t = _num(thickness, eid + " thickness")
    if t <= 0.0:
        fail("%s: roof thickness must be positive" % eid)
    d["pts"] = P
    d["thickness"] = t
    if holes != None:
        H = []
        for k, h in enumerate(holes):
            if type(h) != "list" or len(h) != 4:
                fail("%s: holes[%d] must be [x0, y0, x1, y1], got %r" % (eid, k, h))
            r = [_num(x, "%s holes[%d]" % (eid, k)) for x in h]
            if not (r[0] < r[2] and r[1] < r[3]):
                fail("%s: holes[%d] must run from its minima to its maxima, got %r" % (eid, k, r))
            H.append(r)
        d["holes"] = H
    return d

# ------------------------------------------------------------------ collections

def find(elements, eid):
    """The one element with id `eid`."""
    hits = [e for e in elements if e["id"] == eid]
    if len(hits) != 1:
        fail("expected exactly one element %r, found %d" % (eid, len(hits)))
    return hits[0]

def find_opening(elements, oid):
    """The one opening with id `oid`, in whichever wall holds it."""
    hits = [o for e in elements for o in e.get("openings", []) if o["id"] == oid]
    if len(hits) != 1:
        fail("expected exactly one opening %r, found %d" % (oid, len(hits)))
    return hits[0]

def check_unique_ids(elements):
    """Element ids and opening ids share one namespace and are unique; returns how many there are."""
    seen = {}
    for e in elements:
        for i in [e["id"]] + [o["id"] for o in e.get("openings", [])]:
            if i in seen:
                fail("duplicate id %r: in %s and again in %s" % (i, seen[i], e["id"]))
            seen[i] = e["id"]
    return len(seen)

def require_ids(elements, ids):
    """Fail unless every id in `ids` names an element: the ones the house's checks and photos rely on."""
    have = {e["id"]: True for e in elements}
    missing = [i for i in ids if i not in have]
    if missing:
        fail("the house is missing required element ids %s" % missing)
    return len(ids)

# ------------------------------------------------------------------ checks
#
# Invariants that fail the build rather than produce a quietly wrong model.

def closes_to(what, parts, total, tol = 0.75):
    """A chain of parts sums to a stated total."""
    got = 0.0
    for p in parts:
        got += p
    if abs(got - total) > tol:
        fail("%s: the parts sum to %s but the stated total is %s (off by %s)" % (what, f2(got), f2(total), f2(got - total)))
    return total

def within(what, value, expect, tol):
    """`value` lies within `tol` of `expect`."""
    if abs(value - expect) > tol:
        fail("%s: got %s, expected %s +/- %s" % (what, f2(value), f2(expect), f2(tol)))
    return value
