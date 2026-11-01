load("lib/geometry.star", "glazed_unit", "opening", "rect", "space", "swing", "wall")
load("lib/frame.star", "CEILING", "DOOR_HEAD", "EAST_X", "EXTERIOR_WALL", "FRONT_FACE", "FRONT_Y", "GRADE",
     "INTERIOR_WALL", "LIVING_FACE", "LIVING_KITCHEN_Y", "MAIN_FLOOR", "PARTITION_WEST", "TRIM", "WALL_TOP",
     "WEST_FACE", "WEST_X", "window")
load("zones/roof.star", "ROOF_RIDGE_Y", "roof_underside_z")

# The living room: its floor and ceiling; the front wall, which is the whole front of the house (the front door, the
# living room's window and the bedroom's); the front half of the west gable wall; and the partition to the kitchen with
# its cased opening.
#
# Openings are placed by their start along the wall from its a end and their width. A1 gives no string for the front
# wall's openings, so their jambs are read off its linework through plan_main (segments --layer A-DOOR A-GLAZ), and
# their sills off A2's through elev_front.

LIVING_FRONT_DOOR = [96.0, 36.0]               # A1: the front door's jambs (A-DOOR), from the west outer face
LIVING_FRONT_DOOR_FRAME = {"left": 4.5, "right": 4.5, "bottom": 9.0, "top": 4.5}    # assumed: its stiles and rails
LIVING_FRONT_DOOR_LOCK_RAIL = [36.0, 6.0]      # assumed: the rail between its two lights, centre height and width
LIVING_FRONT_WINDOW = [150.0, 60.0, 30.0]      # A1: start and width (A-GLAZ); A2: sill 30. Three lights (assumed)
LIVING_BEDROOM_WINDOW = [270.0, 48.0, 30.0]    # A1: start and width (A-GLAZ); A2: sill 30. Two lights (assumed)
LIVING_WEST_WINDOW = [36.0, 48.0, 30.0]        # A1: start from the front outer face and width (A-GLAZ); sill assumed
                                               # as the front windows', since A2 shows the front only
LIVING_CASED_OPENING = [144.0, 60.0]           # A1: the gap in the partition (A-WALL), from the west wall's room face

LIVING_ELEMENTS = [
    space("living.space", "living", rect(WEST_FACE, FRONT_FACE, PARTITION_WEST, LIVING_FACE), MAIN_FLOOR,
          ceiling_z = CEILING, floor_finish = "oak", ceiling_finish = "drywall", src = "cad", name = "Living room"),
    wall("living.wall.front", "living", [WEST_X, FRONT_Y + EXTERIOR_WALL / 2.0],
         [EAST_X, FRONT_Y + EXTERIOR_WALL / 2.0], EXTERIOR_WALL, GRADE, WALL_TOP, kind = "exterior",
         faces = {"-y": "siding", "+y": "drywall"}, finish = "siding", src = "cad", name = "Front wall", openings = [
             opening("living.wall.front.door", "door", LIVING_FRONT_DOOR[0], LIVING_FRONT_DOOR[1], MAIN_FLOOR,
                     DOOR_HEAD, swing = swing("start", "left"), casing = TRIM,
                     unit = glazed_unit(frame = LIVING_FRONT_DOOR_FRAME,
                                        horizontals = [LIVING_FRONT_DOOR_LOCK_RAIL[0]],
                                        rail = LIVING_FRONT_DOOR_LOCK_RAIL[1], finish = "wood_mid"),
                     note = "The front door: glass over and under a lock rail, opening into the living room."),
             window("living.wall.front.window", LIVING_FRONT_WINDOW[0], LIVING_FRONT_WINDOW[1],
                    LIVING_FRONT_WINDOW[2], 3),
             window("living.wall.front.window_bedroom", LIVING_BEDROOM_WINDOW[0], LIVING_BEDROOM_WINDOW[1],
                    LIVING_BEDROOM_WINDOW[2], 2),
         ]),
    wall("living.wall.west", "living", [WEST_X + EXTERIOR_WALL / 2.0, FRONT_Y],
         [WEST_X + EXTERIOR_WALL / 2.0, ROOF_RIDGE_Y], EXTERIOR_WALL, GRADE,
         roof_underside_z(FRONT_Y), roof_underside_z(ROOF_RIDGE_Y), kind = "exterior",
         faces = {"-x": "siding", "+x": "drywall"}, finish = "siding", src = "cad",
         name = "West gable wall, front half",
         note = "The gable wall rises to the roof's underside; its back half is kitchen.wall.west.", openings = [
             window("living.wall.west.window", LIVING_WEST_WINDOW[0], LIVING_WEST_WINDOW[1], LIVING_WEST_WINDOW[2], 2),
         ]),
    wall("living.wall.kitchen", "living", [WEST_FACE, LIVING_KITCHEN_Y], [PARTITION_WEST, LIVING_KITCHEN_Y],
         INTERIOR_WALL, MAIN_FLOOR, CEILING, finish = "drywall", src = "cad", name = "Partition to the kitchen",
         openings = [
             opening("living.wall.kitchen.cased", "cased", LIVING_CASED_OPENING[0], LIVING_CASED_OPENING[1],
                     MAIN_FLOOR, DOOR_HEAD, casing = TRIM),
         ]),
]
