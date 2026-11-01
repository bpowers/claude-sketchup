load("lib/geometry.star", "box", "door_seams", "rect", "space", "wall")
load("lib/frame.star", "BACK_FACE", "BACK_Y", "CEILING", "EAST_X", "EXTERIOR_WALL", "GRADE", "KITCHEN_FACE",
     "MAIN_FLOOR", "PARTITION_WEST", "PLAN_BACK", "TRIM", "WALL_TOP", "WEST_FACE", "WEST_X", "window")
load("zones/roof.star", "ROOF_RIDGE_Y", "roof_underside_z")

# The kitchen as it stands: its floor and ceiling; the back wall, which is the whole back of the house (the kitchen's
# window over the sink and the bath's window); the back half of the west gable wall; and today's casework, a run of
# base cabinets and a counter along the back wall with uppers either side of the window and the refrigerator at the
# run's end. The kitchen design area (zones/areas/kitchen.star) builds its alternatives from these.

# A1 dimensions the back wall's windows (PLAN_BACK, lib/frame.star) and draws today's casework (A-FLOR-CASE, the
# uppers dashed) and the refrigerator (A-EQPM) in plan. No sheet shows the back wall or the casework in elevation, so
# their heights are stock dimensions, marked assumed.

KITCHEN_WINDOW = [PLAN_BACK["stations"][1], PLAN_BACK["spans"][1]["len"],
                  42.0]               # A1: start and width; sill assumed, over the backsplash. One light (assumed)
KITCHEN_BATH_WINDOW = [PLAN_BACK["stations"][3], PLAN_BACK["spans"][3]["len"],
                       54.0]          # A1: start and width; sill assumed, high for privacy. One light (assumed)

KITCHEN_BASE_DEPTH = 24.0            # the base cabinets' boxes, wall to front: A1 draws the counter's edge 25.5 from
                                     # the wall (A-FLOR-CASE), this and the overhang together; the split is assumed
KITCHEN_BASE_TOP = 34.5              # assumed: their tops, under the counter
KITCHEN_COUNTER_TOP = 36.0           # assumed: every counter's top
KITCHEN_COUNTER_OVERHANG = 1.5       # assumed: a counter's edge past the cabinet fronts under it
KITCHEN_DRAWER_RAIL = 28.5           # assumed: the line between the base cabinets' drawers and their doors
KITCHEN_BASE_DOOR = 24.0             # assumed: the widest base cabinet door
KITCHEN_UPPER = [54.0, 90.0, 12.0]   # assumed: the uppers' bottom and top; A1: their depth (dashed, A-FLOR-CASE)
KITCHEN_UPPER_DOOR = 18.0            # assumed: the widest upper door
KITCHEN_RUN_END = 150.0              # A1: today's run along the back wall ends here, at the refrigerator
KITCHEN_UPPER_GAP = 0.5              # A1: the uppers stop this far short of the window's casing
KITCHEN_FRIDGE = [36.0, 30.0, 70.0]  # A1: the refrigerator's width and depth (A-EQPM, REF); height assumed
KITCHEN_FREEZER_RAIL = 30.0          # assumed: the top of its freezer drawer

def kitchen_base(eid, x0, x1, y_wall, finish = "cabinet_white", src = None):
    """A run of base cabinets against the wall face at y = y_wall (facing -y), drawers over doors."""
    return box(eid, "kitchen", [x0, y_wall - KITCHEN_BASE_DEPTH, MAIN_FLOOR, x1, y_wall, KITCHEN_BASE_TOP],
               "base_cabinet", front = "-y", seams = door_seams(x1 - x0, KITCHEN_BASE_DOOR),
               rails = [KITCHEN_DRAWER_RAIL], finish = finish, src = src)

def kitchen_counter(eid, x0, x1, y_wall, src = None):
    """The counter over a base run against the wall face at y = y_wall."""
    return box(eid, "kitchen", [x0, y_wall - KITCHEN_BASE_DEPTH - KITCHEN_COUNTER_OVERHANG, KITCHEN_BASE_TOP, x1,
                                y_wall, KITCHEN_COUNTER_TOP], "counter", finish = "counter_stone", src = src)

def kitchen_fridge(x0, src = None):
    """The refrigerator against the back wall from x0, its freezer drawer under its door."""
    w, d, h = KITCHEN_FRIDGE[0], KITCHEN_FRIDGE[1], KITCHEN_FRIDGE[2]
    return box("kitchen.fridge", "kitchen", [x0, BACK_FACE - d, MAIN_FLOOR, x0 + w, BACK_FACE, MAIN_FLOOR + h],
               "appliance", subtype = "fridge", front = "-y", rails = [KITCHEN_FREEZER_RAIL], finish = "stainless",
               src = src)

def kitchen_upper(eid, x0, x1, src = None):
    """Upper cabinets against the back wall from x0 to x1."""
    return box(eid, "kitchen", [x0, BACK_FACE - KITCHEN_UPPER[2], KITCHEN_UPPER[0], x1, BACK_FACE, KITCHEN_UPPER[1]],
               "upper_cabinet", front = "-y", seams = door_seams(x1 - x0, KITCHEN_UPPER_DOOR), finish = "cabinet_white",
               src = src)

# the window's casing, from x to x, which the uppers stop short of
KITCHEN_WINDOW_CASED = [KITCHEN_WINDOW[0] - TRIM["width"], KITCHEN_WINDOW[0] + KITCHEN_WINDOW[1] + TRIM["width"]]

KITCHEN_ELEMENTS = [
    space("kitchen.space", "kitchen", rect(WEST_FACE, KITCHEN_FACE, PARTITION_WEST, BACK_FACE), MAIN_FLOOR,
          ceiling_z = CEILING, floor_finish = "tile_light", ceiling_finish = "drywall", src = "cad",
          name = "Kitchen"),
    wall("kitchen.wall.back", "kitchen", [WEST_X, BACK_Y - EXTERIOR_WALL / 2.0],
         [EAST_X, BACK_Y - EXTERIOR_WALL / 2.0], EXTERIOR_WALL, GRADE, WALL_TOP, kind = "exterior",
         faces = {"+y": "siding", "-y": "drywall"}, finish = "siding", src = "cad", name = "Back wall", openings = [
             window("kitchen.wall.back.window", KITCHEN_WINDOW[0], KITCHEN_WINDOW[1], KITCHEN_WINDOW[2], 1),
             window("kitchen.wall.back.window_bath", KITCHEN_BATH_WINDOW[0], KITCHEN_BATH_WINDOW[1],
                    KITCHEN_BATH_WINDOW[2], 1),
         ]),
    wall("kitchen.wall.west", "kitchen", [WEST_X + EXTERIOR_WALL / 2.0, ROOF_RIDGE_Y],
         [WEST_X + EXTERIOR_WALL / 2.0, BACK_Y], EXTERIOR_WALL, GRADE,
         roof_underside_z(ROOF_RIDGE_Y), roof_underside_z(BACK_Y), kind = "exterior",
         faces = {"-x": "siding", "+x": "drywall"}, finish = "siding", src = "cad",
         name = "West gable wall, back half"),
    kitchen_base("kitchen.base.back", WEST_FACE, KITCHEN_RUN_END, BACK_FACE, src = "cad"),
    kitchen_counter("kitchen.counter.back", WEST_FACE, KITCHEN_RUN_END, BACK_FACE, src = "cad"),
    kitchen_upper("kitchen.upper.left", WEST_FACE, KITCHEN_WINDOW_CASED[0] - KITCHEN_UPPER_GAP, src = "cad"),
    kitchen_upper("kitchen.upper.right", KITCHEN_WINDOW_CASED[1] + KITCHEN_UPPER_GAP, KITCHEN_RUN_END, src = "cad"),
    kitchen_fridge(KITCHEN_RUN_END, src = "cad"),
]
