load("lib/geometry.star", "roof_plane")
load("lib/units.star", "inches")
load("lib/frame.star", "BACK_Y", "EAST_X", "FRONT_Y", "WALL_TOP", "WEST_X")

# The roof: a gable, its ridge along x over the middle of the house, two planes overhanging the walls. The gable walls
# (west and east) rise to its underside, so they load roof_underside_z from here.

ROOF_THICKNESS = 8.0                 # A2: the fascia's depth, read through elev_front; rafters, sheathing and
                                     # shingles as one slab, measured plumb
ROOF_EAVE_OVERHANG = 18.0            # assumed: past the front and back walls' outer faces (A2 shows no section)
ROOF_RAKE_OVERHANG = 12.0            # A2: past the gable walls' outer faces, read through elev_front
ROOF_RIDGE_Y = (FRONT_Y + BACK_Y) / 2.0    # assumed: the ridge over the middle of the depth
ROOF_RIDGE_TOP = inches("15'-4\"")   # A2: RIDGE 15'-4"
ROOF_PITCH = (ROOF_RIDGE_TOP - WALL_TOP - ROOF_THICKNESS) / (ROOF_RIDGE_Y - FRONT_Y)   # from A2's levels: 6 in 12

def roof_top_z(y):
    """The roof's top surface over plan y: it rests on the eave walls' tops at their outer faces and rises at the pitch
    toward the ridge (and falls past the walls, over the overhang)."""
    return WALL_TOP + ROOF_THICKNESS + ROOF_PITCH * min(y - FRONT_Y, BACK_Y - y)

def roof_underside_z(y):
    return roof_top_z(y) - ROOF_THICKNESS

ROOF_RIDGE_Z = roof_top_z(ROOF_RIDGE_Y)

ROOF_X0 = WEST_X - ROOF_RAKE_OVERHANG              # the rakes' edges
ROOF_X1 = EAST_X + ROOF_RAKE_OVERHANG
ROOF_FRONT_EAVE = FRONT_Y - ROOF_EAVE_OVERHANG     # the eaves' edges
ROOF_BACK_EAVE = BACK_Y + ROOF_EAVE_OVERHANG

ROOF_ELEMENTS = [
    roof_plane("roof.plane.front", "roof", [[ROOF_X0, ROOF_FRONT_EAVE, roof_top_z(ROOF_FRONT_EAVE)],
                                           [ROOF_X1, ROOF_FRONT_EAVE, roof_top_z(ROOF_FRONT_EAVE)],
                                           [ROOF_X1, ROOF_RIDGE_Y, ROOF_RIDGE_Z],
                                           [ROOF_X0, ROOF_RIDGE_Y, ROOF_RIDGE_Z]],
               ROOF_THICKNESS, finish = "shingle", src = "cad", name = "Roof, front slope"),
    roof_plane("roof.plane.back", "roof", [[ROOF_X0, ROOF_RIDGE_Y, ROOF_RIDGE_Z],
                                          [ROOF_X1, ROOF_RIDGE_Y, ROOF_RIDGE_Z],
                                          [ROOF_X1, ROOF_BACK_EAVE, roof_top_z(ROOF_BACK_EAVE)],
                                          [ROOF_X0, ROOF_BACK_EAVE, roof_top_z(ROOF_BACK_EAVE)]],
               ROOF_THICKNESS, finish = "shingle", src = "cad", name = "Roof, back slope"),
]
