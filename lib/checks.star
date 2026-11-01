load("lib/geometry.star", "f2", "find", "point_in_polygon", "stair_bottom_z", "wall_length", "within")

# Checks across elements, for house.star: each fails the build on a house that is wrong in a way no single element can
# see (a stair whose foot misses the floor it serves, a post stopping short of its beam, a door hanging above its floor),
# and otherwise returns one line saying what it held, for meta.checks.

def stair_joins(els, sid, upper, lower, top_probe = 3.0):
    """The stair `sid` runs from the floor of the space `upper` down to the floor of the space `lower`: its top and foot
    at their heights, its foot landing in `lower` and its top starting from `upper` (`top_probe` inches up the run from
    the top nosing, past any wall the stair starts at)."""
    s = find(els, sid)
    u = find(els, upper)
    l = find(els, lower)
    within("%s: its top vs the floor of %s" % (sid, upper), s["z_top"], u["floor_z"], 0.01)
    within("%s: its foot vs the floor of %s" % (sid, lower), stair_bottom_z(s), l["floor_z"], 0.01)
    mx = (s["a"][0] + s["b"][0]) / 2.0
    my = (s["a"][1] + s["b"][1]) / 2.0
    dx = s["descend"][0]
    dy = s["descend"][1]
    foot = (s["risers"] - 1) * s["run"] + 3.0
    if not point_in_polygon(l["outline"], mx + dx * foot, my + dy * foot):
        fail("%s: the foot of the stair does not land in %s" % (sid, lower))
    if not point_in_polygon(u["outline"], mx - dx * top_probe, my - dy * top_probe):
        fail("%s: the top of the stair does not start from %s" % (sid, upper))
    return "%s joins %s (z %s) to %s (z %s)" % (sid, upper, f2(u["floor_z"]), lower, f2(l["floor_z"]))

def _top_z(e):
    """The height an element offers to stand on: a slab's top, a space's floor, a beam's top, a box's top."""
    if e["type"] == "slab":
        return e["z_top"]
    if e["type"] == "space":
        return e["floor_z"]
    if e["type"] == "beam":
        return e["soffit_z"] + e["depth"]
    if e["type"] == "box":
        return e["bbox"][5]
    fail("%s: a %s offers nothing to stand on" % (e["id"], e["type"]))

def column_bears(els, cid, base_id, beam_id):
    """The column `cid` stands on `base_id` (a slab, a floor or a box) and carries the beam `beam_id`: its foot at the
    base's top, its top at the beam's soffit, its centre under the beam."""
    c = find(els, cid)
    b = find(els, beam_id)
    within("%s: its foot vs the top of %s" % (cid, base_id), c["z0"], _top_z(find(els, base_id)), 0.01)
    within("%s: its top vs the soffit of %s" % (cid, beam_id), c["z1"], b["soffit_z"], 0.01)
    ux = b["b"][0] - b["a"][0]
    uy = b["b"][1] - b["a"][1]
    length = wall_length(b)
    along = ((c["center"][0] - b["a"][0]) * ux + (c["center"][1] - b["a"][1]) * uy) / length
    across = ((c["center"][0] - b["a"][0]) * -uy + (c["center"][1] - b["a"][1]) * ux) / length
    if along < 0.0 or along > length or abs(across) > b["width"] / 2.0:
        fail("%s: its centre (%s, %s) does not stand under %s" % (cid, f2(c["center"][0]), f2(c["center"][1]), beam_id))
    return "%s stands on %s and carries %s" % (cid, base_id, beam_id)

def _floors_at(spaces, x, y):
    return [s["floor_z"] for s in spaces if point_in_polygon(s["outline"], x, y)]

def sills_on_floors(els, tol = 0.5):
    """Every door's and cased opening's sill is within `tol` of a floor beside it (probed 3 in off either face) or at
    its wall's base. An opening with no space either side is counted, not checked."""
    spaces = [e for e in els if e["type"] == "space"]
    checked = []
    unchecked = []
    for w in els:
        if w["type"] != "wall":
            continue
        length = wall_length(w)
        ux = (w["b"][0] - w["a"][0]) / length
        uy = (w["b"][1] - w["a"][1]) / length
        off = w["thickness"] / 2.0 + 3.0
        for o in w["openings"]:
            if o["kind"] not in ["door", "cased"]:
                continue
            s = o["at"] + o["width"] / 2.0
            px = w["a"][0] + ux * s
            py = w["a"][1] + uy * s
            floors = _floors_at(spaces, px - uy * off, py + ux * off) + _floors_at(spaces, px + uy * off, py - ux * off)
            if not floors:
                unchecked.append(o["id"])
                continue
            if not [f for f in floors if abs(o["sill_z"] - f) <= tol] and abs(o["sill_z"] - w["z0"]) > 0.01:
                fail("%s: its sill %s is neither within %s in of a floor beside it (%s) nor at the base of %s (%s)" % (
                    o["id"], f2(o["sill_z"]), f2(tol), ", ".join([f2(f) for f in floors]), w["id"], f2(w["z0"])))
            checked.append(o["id"])
    out = "door and cased sills: %d stand on a floor beside them" % len(checked)
    return out + ("; %d have no space either side (%s)" % (len(unchecked), ", ".join(unchecked)) if unchecked else "")

def all_at(els, ids, key, value, what):
    """Each element in `ids` has `key` at `value`: every room's floor on the main floor datum, say."""
    for i in ids:
        within("%s: %s %s vs %s" % (i, key, f2(find(els, i)[key]), what), find(els, i)[key], value, 0.01)
    return "%s: %s of %s at z %s" % (what, key, ", ".join(ids), f2(value))
