"""Plan and section geometry shared by the planner and by anything that reads house.json.

Pure functions on plain lists and dicts, in the document's frame (inches, z absolute). The element helpers at the end
take planned elements (ir.plan), whose defaults are filled in and whose openings carry their resolved rectangles, so a
consumer never re-derives what the planner decided:

    from ir import plan, geom
    p = plan.plan_document(json.load(open("house.json")))
    wall = next(e for e in p.elements if e["id"] == "living.wall.south")
    geom.wall_point(wall, d=24.0, z=36.0, off=wall["thickness"] / 2)   # a point on its left face
"""
from __future__ import annotations

import math

EPS = 0.001          # in: SketchUp merges vertices closer than this, so it is the planner's tolerance too


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def is_point(v, n=2) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == n and all(is_number(c) for c in v)


def dist(p, q) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(p, q)))


def cross2(a, b, c) -> float:
    """The z of (b - a) x (c - a): positive when a -> b -> c turns counterclockwise."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def area2(pts) -> float:
    """Signed plan area of a polygon, positive when it runs counterclockwise."""
    s = 0.0
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        s += p[0] * q[1] - q[0] * p[1]
    return s / 2.0


def newell(pts):
    """The (unnormalised) normal of a 3D polygon by Newell's method; robust to slightly non-planar points."""
    nx = ny = nz = 0.0
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        nx += (p[1] - q[1]) * (p[2] + q[2])
        ny += (p[2] - q[2]) * (p[0] + q[0])
        nz += (p[0] - q[0]) * (p[1] + q[1])
    return nx, ny, nz


def line_dist(p, r, q) -> float:
    """Distance from q to the line through p and r (any dimension)."""
    pr = [b - a for a, b in zip(p, r)]
    l2 = sum(v * v for v in pr)
    if l2 < 1e-18:
        return dist(p, q)
    t = sum((c - a) * v for a, c, v in zip(p, q, pr)) / l2
    return dist([a + v * t for a, v in zip(p, pr)], q)


def point_segment_distance(p, a, b) -> float:
    """Plan distance from p to the segment a -> b."""
    abx, aby = b[0] - a[0], b[1] - a[1]
    l2 = abx * abx + aby * aby
    t = 0.0 if l2 < 1e-18 else min(max(((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / l2, 0.0), 1.0)
    return math.hypot(p[0] - (a[0] + abx * t), p[1] - (a[1] + aby * t))


def segments_touch(p1, p2, p3, p4) -> bool:
    """Whether two plan segments cross or touch (within EPS where they meet end to side)."""
    def on(a, b, p):
        return (min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS and
                min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS)
    d1, d2 = cross2(p3, p4, p1), cross2(p3, p4, p2)
    d3, d4 = cross2(p1, p2, p3), cross2(p1, p2, p4)
    tiny = 1e-9
    if ((d1 > tiny and d2 < -tiny) or (d1 < -tiny and d2 > tiny)) and \
            ((d3 > tiny and d4 < -tiny) or (d3 < -tiny and d4 > tiny)):
        return True
    return ((abs(d1) <= tiny and on(p3, p4, p1)) or (abs(d2) <= tiny and on(p3, p4, p2)) or
            (abs(d3) <= tiny and on(p1, p2, p3)) or (abs(d4) <= tiny and on(p1, p2, p4)))


def segment_distance(p1, p2, q1, q2) -> float:
    """Plan distance between two segments; 0 where they cross."""
    d1, d2 = cross2(q1, q2, p1), cross2(q1, q2, p2)
    d3, d4 = cross2(p1, p2, q1), cross2(p1, p2, q2)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return 0.0
    return min(point_segment_distance(p1, q1, q2), point_segment_distance(p2, q1, q2),
               point_segment_distance(q1, p1, p2), point_segment_distance(q2, p1, p2))


def self_touch(pts):
    """(i, j) for the first two non-adjacent edges of a closed polygon that touch, or None when it is simple."""
    n = len(pts)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if segments_touch(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                return i, j
    return None


def inside_polygon(poly, q) -> bool:
    """Whether plan point q lies inside a simple polygon of either orientation (even-odd rule)."""
    inside = False
    n = len(poly)
    for k in range(n):
        a, b = poly[k], poly[(k + 1) % n]
        if (a[1] > q[1]) == (b[1] > q[1]):
            continue
        if q[0] < a[0] + (q[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
            inside = not inside
    return inside


def inside_convex(poly, q) -> bool:
    """Whether q lies inside or on a convex polygon of either orientation."""
    sides = [cross2(poly[k], poly[(k + 1) % len(poly)], q) for k in range(len(poly))]
    return all(c >= -1e-9 for c in sides) or all(c <= 1e-9 for c in sides)


def segment_polygon_distance(p1, p2, poly) -> float:
    """Plan distance from a segment to a convex polygon; 0 where they meet."""
    if inside_convex(poly, p1) or inside_convex(poly, p2):
        return 0.0
    return min(segment_distance(p1, p2, poly[k], poly[(k + 1) % len(poly)]) for k in range(len(poly)))


def polygon_distance(p, q) -> float:
    """Plan distance between two convex polygons; 0 where they meet."""
    if any(inside_convex(q, v) for v in p) or any(inside_convex(p, v) for v in q):
        return 0.0
    return min(segment_distance(p[i], p[(i + 1) % len(p)], q[j], q[(j + 1) % len(q)])
               for i in range(len(p)) for j in range(len(q)))


def clean_ring(pts, dim=2):
    """A polygon's corners as SketchUp will take them: points within EPS of the one before merged, a repeated closing
    point dropped, corners on a straight run removed. Raises ValueError for a spike (an outline that doubles back) or
    fewer than three corners left."""
    out = []
    for p in pts:
        if not (out and dist(out[-1], p) <= EPS):
            out.append(list(p))
    while len(out) > 1 and dist(out[0], out[-1]) <= EPS:
        out.pop()
    changed = True
    while changed and len(out) >= 3:
        changed = False
        n = len(out)
        for i in range(n):
            p, q, r = out[(i - 1) % n], out[i], out[(i + 1) % n]
            if line_dist(p, r, q) > 1e-6:
                continue
            if sum((b - a) * (c - b) for a, b, c in zip(p, q, r)) < 0:
                raise ValueError("doubles back on itself at " + ", ".join(f"{v:g}" for v in q))
            del out[i]
            changed = True
            break
    if len(out) < 3:
        raise ValueError("has fewer than 3 distinct corners")
    return [p[:dim] for p in out]


def rect_corners(r):
    """[x0, y0, x1, y1] as its four plan corners, counterclockwise."""
    x0, y0, x1, y1 = r
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# ------------------------------------------------------------------ elements (planned, see ir.plan)

def line_frame(a, b):
    """(ux, uy, length) of the plan segment a -> b. Its left normal, used for a wall's left face, is (-uy, ux)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return dx / length, dy / length, length


def wall_point(wall, d, z, off=0.0):
    """The point d along a wall's centreline from a, at height z, `off` toward its left face (a -> b turned +90
    degrees); off = thickness/2 is on the left face, -thickness/2 on the right."""
    ux, uy, _ = line_frame(wall["a"], wall["b"])
    ax, ay = wall["a"]
    return [ax + ux * d - uy * off, ay + uy * d + ux * off, z]


def wall_local(wall, p):
    """(d, off): where plan point p lies against a wall: along its centreline from a, and toward its left face."""
    ux, uy, _ = line_frame(wall["a"], wall["b"])
    dx, dy = p[0] - wall["a"][0], p[1] - wall["a"][1]
    return dx * ux + dy * uy, -dx * uy + dy * ux


def wall_top_at(wall, d) -> float:
    """A wall's top d along it from a: straight from top_a to top_b."""
    length = wall.get("length") or line_frame(wall["a"], wall["b"])[2]
    return wall["top_a"] + (wall["top_b"] - wall["top_a"]) * d / length


def wall_top_range(wall, d0, d1):
    """(lowest, highest) of a wall's top over d0..d1 along it."""
    zs = (wall_top_at(wall, d0), wall_top_at(wall, d1))
    return min(zs), max(zs)


def ceiling_at(space, x, y):
    """The height of a space's ceiling over plan point (x, y), or None when it has none."""
    plane = space.get("ceiling_plane")
    if plane:
        (ax, ay, az), (sx, sy) = plane["at"], plane["slope"]
        return az + sx * (x - ax) + sy * (y - ay)
    cz = space.get("ceiling_z")
    return float(cz) if is_number(cz) else None


def ceiling_range(space):
    """(lowest, highest) of a space's ceiling over its outline, or None when it has none (a plane's extremes over a
    polygon lie at its corners)."""
    zs = [ceiling_at(space, x, y) for x, y in space["outline"]]
    return None if zs[0] is None else (min(zs), max(zs))


def space_at(spaces, x, y):
    """The spaces whose outline holds plan point (x, y), in the order given."""
    return [sp for sp in spaces if inside_polygon(sp["outline"], (x, y))]


def roof_top_z(roof, x, y) -> float:
    """The z of a roof plane's top face over plan point (x, y), on the plane through its pts."""
    pts = roof["pts"]
    nx, ny, nz = newell(pts)
    cx, cy, cz = (sum(p[k] for p in pts) / len(pts) for k in range(3))
    return cz - (nx * (x - cx) + ny * (y - cy)) / nz


def stair_treads(stair):
    """[(z, d0, d1), ...]: each tread of a stair, its height and its span in from the top nosing line along
    `descend`; tread k (1 .. risers-1) stands at z_top - k*rise over (k-1)*run .. k*run. The lower floor is at
    z_top - risers*rise."""
    return [(stair["z_top"] - k * stair["rise"], (k - 1) * stair["run"], k * stair["run"])
            for k in range(1, stair["risers"])]


def element_zrange(el):
    """(lowest, highest) z of a planned element."""
    t = el["type"]
    if t == "space":
        rng = ceiling_range(el)
        return el["floor_z"], (rng[1] if rng else el["floor_z"])
    if t == "slab":
        return el["z_top"] - el["thickness"], el["z_top"]
    if t == "wall":
        return el["z0"], max(el["top_a"], el["top_b"])
    if t == "column":
        return el["z0"], el["z1"]
    if t == "beam":
        return el["soffit_z"], el["soffit_z"] + el["depth"]
    if t == "stair":
        return el["z_top"] - el["risers"] * el["rise"], el["z_top"]
    if t == "box":
        return el["bbox"][2], el["bbox"][5]
    if t == "roof_plane":
        zs = [p[2] for p in el["pts"]]
        return min(zs) - el["thickness"], max(zs)
    raise ValueError(f"unknown type {t!r}")
