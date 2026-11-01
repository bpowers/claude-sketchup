load("lib/geometry.star", "rect", "space", "wall")
load("lib/frame.star", "BACK_FACE", "BACK_Y", "BATH_FACE", "BEDROOM_BATH_Y", "CEILING", "EAST_FACE", "EAST_X",
     "EXTERIOR_WALL", "GRADE", "INTERIOR_WALL", "MAIN_FLOOR", "PARTITION_EAST", "interior_door")
load("zones/roof.star", "ROOF_RIDGE_Y", "roof_underside_z")

# The bath: its floor and ceiling; the back half of the east gable wall; and the partition between the bedroom and the
# bath, with the bath's door. Its window is in the back wall (kitchen.wall.back.window_bath).

BATH_DOOR = [30.0, 28.0]             # A1: the jambs from the partition's bedroom-side face (A-DOOR); its leaf
                                     # swings into the bath

BATH_ELEMENTS = [
    space("bath.space", "bath", rect(PARTITION_EAST, BATH_FACE, EAST_FACE, BACK_FACE), MAIN_FLOOR,
          ceiling_z = CEILING, floor_finish = "tile_light", ceiling_finish = "drywall", src = "cad", name = "Bath"),
    wall("bath.wall.east", "bath", [EAST_X - EXTERIOR_WALL / 2.0, ROOF_RIDGE_Y], [EAST_X - EXTERIOR_WALL / 2.0, BACK_Y],
         EXTERIOR_WALL, GRADE, roof_underside_z(ROOF_RIDGE_Y), roof_underside_z(BACK_Y), kind = "exterior",
         faces = {"+x": "siding", "-x": "drywall"}, finish = "siding", src = "cad",
         name = "East gable wall, back half"),
    # a -> b runs +x, so the bath (+y) is to its left
    wall("bath.wall.bedroom", "bath", [PARTITION_EAST, BEDROOM_BATH_Y], [EAST_FACE, BEDROOM_BATH_Y], INTERIOR_WALL,
         MAIN_FLOOR, CEILING, finish = "drywall", src = "cad", name = "Partition between the bedroom and the bath",
         openings = [interior_door("bath.wall.bedroom.door", BATH_DOOR[0], BATH_DOOR[1], "start", "left")]),
]
