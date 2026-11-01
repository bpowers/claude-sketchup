load("lib/geometry.star", "casing", "glazed_unit", "leaf_unit", "opening", "split_lights", "swing")
load("lib/units.star", "chain", "inches")

# The house's frame: the axes, the plan's grid lines, the heights and the details every zone builds from, and the
# drawing sheets registered into it.
#
# +x runs along the front wall, left to right as seen from the street; +y runs from the front wall toward the back;
# +z is up. z = 0 is the main floor's finish floor. Inches throughout, and every z is absolute. The cottage's front
# faces south, so +y is north (house.star's LOCATION has north_angle 0) and its west wall stands at x = 0, its east
# wall at the far end of +x.
#
# Each number cites where it comes from. The example cottage's sheets, sources/plans/example-cottage.pdf (A1 the floor
# plan, A2 the front elevation), are synthetic: they were drawn from these numbers to give the drawing tools something
# to run on, so they agree with them exactly.

# ------------------------------------------------------------------ drawing sheets
#
# A registration maps a point on a drawing sheet (PDF points, y up, as extract/pdf.py reports them) into the model's
# frame: model = rotate((pt - origin_pt) * in_per_pt, rotation_deg), counter-clockwise. A point read off a sheet that
# no dimension states (a post's centre, a jog in a wall) is cited as the sheet point itself, sheet_to_model(PLAN_MAIN,
# [x_pt, y_pt]), so a corrected registration moves every number taken from that sheet. The Python tools read the
# registrations from sources/plans/registrations.json and Starlark cannot read files, so each one Starlark uses is
# mirrored here, named for it in capitals (plan_main is PLAN_MAIN); extract.pdf calibrate --register prints the line,
# and tests/test_pdf.py fails if the two copies differ.

PI = 3.141592653589793

def _cos_sin(deg):
    """[cos, sin] of an angle in degrees: exact at multiples of 90, a series elsewhere (Starlark has no math module)."""
    q = deg / 90.0
    if q == int(q):
        return [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [0.0, -1.0]][int(q) % 4]
    t = deg * PI / 180.0
    t = t - 2.0 * PI * int((t + PI) / (2.0 * PI))
    c, s, tc, ts = 1.0, t, 1.0, t
    for k in range(1, 20):
        tc = -tc * t * t / ((2 * k - 1) * (2 * k))
        ts = -ts * t * t / ((2 * k) * (2 * k + 1))
        c += tc
        s += ts
    return [c, s]

def registration(origin_pt, in_per_pt, rotation_deg = 0.0):
    """A drawing sheet's placement in the model: the sheet point that lands on the model's origin, the scale in model
    inches per sheet point, and the turn counter-clockwise from the sheet's axes to the model's."""
    if type(origin_pt) != "list" or len(origin_pt) != 2:
        fail("registration: origin_pt must be [x, y] in points, got %r" % (origin_pt,))
    if not (type(in_per_pt) in ["int", "float"] and in_per_pt > 0):
        fail("registration: in_per_pt must be a positive number, got %r" % (in_per_pt,))
    return {"origin_pt": [float(origin_pt[0]), float(origin_pt[1])], "scale": float(in_per_pt),
            "rotation_deg": float(rotation_deg), "cos_sin": _cos_sin(float(rotation_deg))}

def _thousandth(v):
    """v to the nearest 0.001 in, SketchUp's tolerance: no sheet is read finer, and the scale's float noise stays out of
    house.json."""
    return int(v * 1000.0 + (0.5 if v >= 0 else -0.5)) / 1000.0

def sheet_to_model(reg, pt):
    """A sheet point [x_pt, y_pt] as model [x, y] inches through a registration(...)."""
    dx = (pt[0] - reg["origin_pt"][0]) * reg["scale"]
    dy = (pt[1] - reg["origin_pt"][1]) * reg["scale"]
    c, s = reg["cos_sin"][0], reg["cos_sin"][1]
    return [_thousandth(c * dx - s * dy), _thousandth(s * dx + c * dy)]

# A1, the floor plan: sheet page 0, 1/4" = 1'-0", the west and front walls' outer corner at (300, 220) pt.
PLAN_MAIN = registration([300.0, 220.0], 0.66666667)

# ------------------------------------------------------------------ the plan's grid
#
# Dimension strings as A1 writes them: to the exterior walls' outer faces and the partitions' centrelines. Each must
# close on its overall, so a misread dimension fails the build here.

PLAN_X = chain([["living room and kitchen", "20'-0\""], ["bedroom and bath", "10'-0\""]],
               closes = "30'-0\"", name = "A1, the front wall's string")
PLAN_Y_WEST = chain([["living room", "11'-0\""], ["kitchen", "FILL"]],
                    closes = "24'-0\"", name = "A1, the west wall's string")
PLAN_Y_EAST = chain([["bedroom", "14'-0\""], ["bath", "FILL"]],
                    closes = "24'-0\"", name = "A1, the east wall's string")
PLAN_BACK = chain([["to the kitchen window", "5'-6\""], ["the kitchen window", "3'-0\""],
                   ["to the bath window", "16'-6\""], ["the bath window", "2'-6\""], ["to the east face", "2'-6\""]],
                  closes = "30'-0\"", name = "A1, the back wall's string")

WEST_X = PLAN_X["stations"][0]             # the west wall's outer face
PARTITION_X = PLAN_X["stations"][1]        # the centreline of the partition between the living side and the bedroom side
EAST_X = PLAN_X["stations"][2]             # the east wall's outer face
FRONT_Y = PLAN_Y_WEST["stations"][0]       # the front wall's outer face
LIVING_KITCHEN_Y = PLAN_Y_WEST["stations"][1]   # the centreline of the partition between the living room and kitchen
BEDROOM_BATH_Y = PLAN_Y_EAST["stations"][1]     # the centreline of the partition between the bedroom and bath
BACK_Y = PLAN_Y_WEST["stations"][2]        # the back wall's outer face

EXTERIOR_WALL = 6.0                        # A1: the exterior walls' faces, 9 pt apart (A-WALL); siding, sheathing, 2x4
                                           # studs and drywall
INTERIOR_WALL = 4.5                        # A1: the partitions' faces (A-WALL); 2x4 studs and drywall both sides

# The room faces, which the spaces' outlines and the casework stand on.
WEST_FACE = WEST_X + EXTERIOR_WALL
EAST_FACE = EAST_X - EXTERIOR_WALL
FRONT_FACE = FRONT_Y + EXTERIOR_WALL
BACK_FACE = BACK_Y - EXTERIOR_WALL
PARTITION_WEST = PARTITION_X - INTERIOR_WALL / 2.0      # the partition's living-side face
PARTITION_EAST = PARTITION_X + INTERIOR_WALL / 2.0      # its bedroom-side face
LIVING_FACE = LIVING_KITCHEN_Y - INTERIOR_WALL / 2.0    # the living room's face of the kitchen partition
KITCHEN_FACE = LIVING_KITCHEN_Y + INTERIOR_WALL / 2.0   # the kitchen's face of it
BEDROOM_FACE = BEDROOM_BATH_Y - INTERIOR_WALL / 2.0
BATH_FACE = BEDROOM_BATH_Y + INTERIOR_WALL / 2.0

# ------------------------------------------------------------------ heights

MAIN_FLOOR = 0.0                           # A2: FIN. FLOOR 0'-0", the main floor's finish floor
CEILING = MAIN_FLOOR + inches("8'-0\"")    # A1: CLG 8'-0" in every room, a flat ceiling
WALL_TOP = MAIN_FLOOR + inches("8'-8\"")   # A2: T.O. PLATE 8'-8", the exterior walls' top plate under the roof
GRADE = MAIN_FLOOR - 18.0                  # A2: the grade line, read through elev_front; the exterior walls run down
                                           # to it, so their faces include the foundation's
RISER = 7.0                                # A2: the front steps, two risers from the porch to the floor, read through
                                           # elev_front
DOOR_HEAD = inches("6'-8\"")               # A2: the door's and the windows' heads, read through elev_front

# ------------------------------------------------------------------ the details every zone shares

WINDOW_FRAME = 2.0                         # assumed: the windows' frame members (no sheet draws the units)
WINDOW_MULLION = 1.5                       # assumed: and their mullions
TRIM = casing(width = 3.5, finish = "trim_white")    # assumed: every door's and window's casing, on both faces

def window(oid, at, width, sill, lights):
    """One of the house's windows: `lights` equal lights side by side between DOOR_HEAD and `sill`, cased both faces."""
    return opening(oid, "window", at, width, sill, DOOR_HEAD, casing = TRIM, unit = glazed_unit(
        frame = WINDOW_FRAME, verticals = split_lights(WINDOW_FRAME, width - WINDOW_FRAME, lights, WINDOW_MULLION),
        mullion = WINDOW_MULLION, finish = "trim_white"))

def interior_door(oid, at, width, hinge, side):
    """One of the house's interior doors: a painted panel leaf on the main floor, cased both faces, swinging toward
    `side` from its `hinge` jamb (swing())."""
    return opening(oid, "door", at, width, MAIN_FLOOR, DOOR_HEAD, swing = swing(hinge, side), casing = TRIM,
                   unit = leaf_unit(finish = "trim_white", leaf_finish = "paint_white"))
