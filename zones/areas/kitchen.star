load("lib/geometry.star", "box", "f2")
load("lib/frame.star", "BACK_FACE", "KITCHEN_FACE", "MAIN_FLOOR", "PARTITION_WEST", "WEST_FACE")
load("zones/kitchen.star", "KITCHEN_BASE_DEPTH", "KITCHEN_BASE_TOP", "KITCHEN_COUNTER_OVERHANG", "KITCHEN_COUNTER_TOP",
     "KITCHEN_FRIDGE", "kitchen_base", "kitchen_counter", "kitchen_fridge")

# The kitchen design: the alternatives of the kitchen area (zones/areas/kitchen/*.star) are cut from this one design,
# each passing the choices it differs in (KITCHEN_CHOICES) to kitchen_layer().
#
# Both alternatives move the refrigerator to the end wall and carry today's run of base cabinets and counter on along
# the back wall to it. Then a block of base cabinets under a counter stands across the kitchen, its work side facing
# the back run across the aisle and stools along its living room side: free as an island, or running off the west wall
# as a peninsula. The uppers stay or go.
#
# Every number the design decides is a named constant below, with the owner's words beside it: "make the island 6 in
# longer" is one number, and the design's elements carry src = "owner". The rules it must hold are constants too, and
# kitchen_layer() refuses a design that breaks one with a stable code ("kitchen aisle: ..."), so the compile says which
# rule and why. What it measured goes into the layer's checks, one line of data each: "<code> <value> <op> <limit> ok".

# ------------------------------------------------------------------ the rules
AISLE_MIN = 42.0                 # aisle: the working aisle between two counters (kitchen-planning guidance, one cook)
WALKWAY_MIN = 36.0               # walkway: a way past a counter's end or behind its stools

# ------------------------------------------------------------------ the decisions
FRIDGE_X = PARTITION_WEST - KITCHEN_FRIDGE[0]    # the refrigerator against the end wall. The owner: "move the fridge
                                                 # to the end wall, out of the way of the cooking"
BLOCK_WALKWAY = 40.0             # from the living room partition to the block's counter edge. The owner: "keep a good
                                 # walkway in from the living room"
SEAT_OVERHANG = 12.0             # the counter past the block's cabinets on the stool side. The owner: "knee room for
                                 # stools facing the living room"
ISLAND_LENGTH = 84.0             # the island's counter. The owner: "about seven feet long"
ISLAND_END_WALKWAY = 48.0        # from the west wall to the island's end. The owner: "walk all the way round it"
PENINSULA_LENGTH = 96.0          # the peninsula's counter from the west wall. The owner: "eight feet, off the wall"
STOOL_WIDTH = 24.0               # the counter each stool takes

# The choices an alternative passes (with its id and name), and their values unless it says otherwise.
KITCHEN_CHOICES = {
    "block": "island",           # BLOCKS: what stands across the kitchen
    "uppers": True,              # today's uppers stay
}
BLOCKS = ["island", "peninsula"]

def kitchen_fail(code, words):
    """Refuse the design with a stable code: tests and readers match the code, never the words."""
    fail("kitchen %s: %s" % (code, words))

def _value(v):
    return "-" if v == None else (str(v) if type(v) == "int" else f2(v))

def _says(out, code, value, op = "-", limit = None):
    """One check line: "<code> <value> <op> <limit> ok", a measurement where op and limit are "-"."""
    out.append("%s %s %s %s ok" % (code, _value(value), op, _value(limit)))

def _at_least(out, code, value, limit):
    """`value` must be at least `limit`: the design is refused under `code` where it is not, and says so where it is."""
    if value < limit - 0.005:
        kitchen_fail(code, "%s in, under the %s in minimum" % (f2(value), f2(limit)))
    _says(out, code, value, ">=", limit)

def kitchen_layer(choices):
    """The LAYER of a kitchen alternative: `choices` holds its id and name and any of KITCHEN_CHOICES's keys."""
    for k in choices:
        if k not in KITCHEN_CHOICES and k not in ["id", "name"]:
            kitchen_fail("unknown_choice", "%r is not a choice (choices: %s)" % (k, ", ".join(sorted(KITCHEN_CHOICES))))
    c = dict(KITCHEN_CHOICES, **choices)
    if c["block"] not in BLOCKS:
        kitchen_fail("block", "the block is one of %s, not %r" % (", ".join(BLOCKS), c["block"]))
    checks = []

    # the back run, carried on to the refrigerator at the end wall
    replaces = [
        kitchen_base("kitchen.base.back", WEST_FACE, FRIDGE_X, BACK_FACE, src = "owner"),
        kitchen_counter("kitchen.counter.back", WEST_FACE, FRIDGE_X, BACK_FACE, src = "owner"),
        kitchen_fridge(FRIDGE_X, src = "owner"),
    ]
    run_edge = BACK_FACE - KITCHEN_BASE_DEPTH - KITCHEN_COUNTER_OVERHANG

    # the block: stools along its living room side, its cabinets facing the back run
    if c["block"] == "island":
        x0 = WEST_FACE + ISLAND_END_WALKWAY
        x1 = x0 + ISLAND_LENGTH
        base_x = [x0 + KITCHEN_COUNTER_OVERHANG, x1 - KITCHEN_COUNTER_OVERHANG]
    else:
        x0 = WEST_FACE
        x1 = x0 + PENINSULA_LENGTH
        base_x = [x0, x1 - KITCHEN_COUNTER_OVERHANG]
    seat_edge = KITCHEN_FACE + BLOCK_WALKWAY
    base_y = [seat_edge + SEAT_OVERHANG, seat_edge + SEAT_OVERHANG + KITCHEN_BASE_DEPTH]
    work_edge = base_y[1] + KITCHEN_COUNTER_OVERHANG
    block = c["block"]
    adds = [
        box("kitchen.%s.base" % block, "kitchen", [base_x[0], base_y[0], MAIN_FLOOR, base_x[1], base_y[1],
                                                   KITCHEN_BASE_TOP],
            "base_cabinet", front = "+y", seams = [(base_x[1] - base_x[0]) / 2.0], finish = "cabinet_wood",
            src = "owner", name = "The %s's cabinets" % block),
        box("kitchen.%s.counter" % block, "kitchen", [x0, seat_edge, KITCHEN_BASE_TOP, x1, work_edge,
                                                      KITCHEN_COUNTER_TOP],
            "counter", finish = "counter_stone", src = "owner", name = "The %s's counter" % block),
    ]

    # the rules, and what the design measured
    _at_least(checks, "aisle", run_edge - work_edge, AISLE_MIN)
    _at_least(checks, "walkway.stools", seat_edge - KITCHEN_FACE, WALKWAY_MIN)
    if block == "island":
        _at_least(checks, "walkway.west_end", x0 - WEST_FACE, WALKWAY_MIN)
    _at_least(checks, "walkway.east_end", PARTITION_WEST - x1, WALKWAY_MIN)
    _says(checks, "counter.length", x1 - x0)
    _says(checks, "seats", int((x1 - x0) / STOOL_WIDTH))

    return {
        "id": c["id"],
        "name": c["name"],
        "note": "kitchen " + " ".join(["%s=%s" % (k, c[k]) for k in sorted(KITCHEN_CHOICES)]),
        "removes": [] if c["uppers"] else ["kitchen.upper.left", "kitchen.upper.right"],
        "replaces": replaces,
        "adds": adds,
        "checks": checks,
        "later": ["the stools", "pendant lights over the %s" % block],
    }
