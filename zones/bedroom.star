load("lib/geometry.star", "rect", "space", "wall")
load("lib/frame.star", "BACK_FACE", "BEDROOM_FACE", "CEILING", "EAST_FACE", "EAST_X", "EXTERIOR_WALL", "FRONT_FACE",
     "FRONT_Y", "GRADE", "INTERIOR_WALL", "MAIN_FLOOR", "PARTITION_EAST", "PARTITION_X", "interior_door", "window")
load("zones/roof.star", "ROOF_RIDGE_Y", "roof_underside_z")

# The bedroom: its floor and ceiling; the front half of the east gable wall with the bedroom's side window; and the
# partition between the living side and the bedroom side, the whole depth of the house, with the bedroom's door.

BEDROOM_EAST_WINDOW = [60.0, 48.0, 30.0]  # A1: start from the front outer face and width (A-GLAZ); sill assumed as
                                          # the front windows'. Two lights (assumed)
BEDROOM_DOOR = [84.0, 32.0]          # A1: the jambs from the front wall's room face (A-DOOR); its leaf hinges on the
                                     # front jamb and swings into the bedroom

BEDROOM_ELEMENTS = [
    space("bedroom.space", "bedroom", rect(PARTITION_EAST, FRONT_FACE, EAST_FACE, BEDROOM_FACE), MAIN_FLOOR,
          ceiling_z = CEILING, floor_finish = "carpet", ceiling_finish = "drywall", src = "cad", name = "Bedroom"),
    wall("bedroom.wall.east", "bedroom", [EAST_X - EXTERIOR_WALL / 2.0, FRONT_Y],
         [EAST_X - EXTERIOR_WALL / 2.0, ROOF_RIDGE_Y], EXTERIOR_WALL, GRADE,
         roof_underside_z(FRONT_Y), roof_underside_z(ROOF_RIDGE_Y), kind = "exterior",
         faces = {"+x": "siding", "-x": "drywall"}, finish = "siding", src = "cad",
         name = "East gable wall, front half",
         note = "The gable wall rises to the roof's underside; its back half is bath.wall.east.", openings = [
             window("bedroom.wall.east.window", BEDROOM_EAST_WINDOW[0], BEDROOM_EAST_WINDOW[1],
                    BEDROOM_EAST_WINDOW[2], 2),
         ]),
    # a -> b runs +y, so the bedroom (+x) is to its right
    wall("bedroom.wall.partition", "bedroom", [PARTITION_X, FRONT_FACE], [PARTITION_X, BACK_FACE], INTERIOR_WALL,
         MAIN_FLOOR, CEILING, finish = "drywall", src = "cad",
         name = "Partition between the living side and the bedroom side",
         openings = [interior_door("bedroom.wall.partition.door", BEDROOM_DOOR[0], BEDROOM_DOOR[1], "start", "right")]),
]
