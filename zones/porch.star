load("lib/geometry.star", "beam", "column", "rect", "slab", "space", "stair")
load("lib/frame.star", "FRONT_Y", "GRADE", "MAIN_FLOOR", "PLAN_MAIN", "RISER", "sheet_to_model")
load("zones/living.star", "LIVING_FRONT_DOOR")

# The porch, outdoors: a concrete slab two risers below the main floor, the steps from the front door down to it, and
# a post-and-beam pergola over its east end, the beam borne by the front wall and a post.

PORCH_RISERS = 2                     # A2: from the front door's sill down to the porch
PORCH_TOP = MAIN_FLOOR - PORCH_RISERS * RISER
PORCH = [48.0, 180.0, 72.0]          # A1: the slab's west and east edges, and its depth out from the front wall
                                     # (A-FLOR-OTLN)
PORCH_STEP_RUN = 11.0                # A1: the steps' tread (A-FLOR-STRS)
PORCH_STEP_WIDTH = 48.0              # A1: centred on the front door (A-FLOR-STRS)
PORCH_POST_AT = sheet_to_model(PLAN_MAIN, [561.0, 121.0])    # the pergola post's centre: A1 draws it (layer A-COLS)
                                                            # and no dimension locates it, so it is read off the sheet
PORCH_POST_SIZE = 5.5                # A1: the post's square section (A-COLS)
PORCH_POST_CAP = {"size": [7.5, 7.5], "height": 3.5}    # A2: the post's cap, read through elev_front
PORCH_BEAM = [5.5, 9.25, 84.0]       # A1: the pergola beam's width (A-BEAM); A2: its depth and soffit

PORCH_OUTLINE = rect(PORCH[0], FRONT_Y - PORCH[2], PORCH[1], FRONT_Y)
PORCH_STEPS_X = LIVING_FRONT_DOOR[0] + LIVING_FRONT_DOOR[1] / 2.0 - PORCH_STEP_WIDTH / 2.0

PORCH_ELEMENTS = [
    slab("porch.slab", "porch", PORCH_OUTLINE, PORCH_TOP, PORCH_TOP - GRADE, finish = "concrete", src = "cad",
         name = "Porch slab", note = "It runs down to grade."),
    space("porch.space", "porch", PORCH_OUTLINE, PORCH_TOP, build_floor = False, src = "cad", name = "Porch",
          note = "The slab draws the floor; the space says where outdoors is."),
    stair("porch.stair", "porch", [PORCH_STEPS_X, FRONT_Y], [PORCH_STEPS_X + PORCH_STEP_WIDTH, FRONT_Y], [0.0, -1.0],
          MAIN_FLOOR, PORCH_RISERS, RISER, PORCH_STEP_RUN, finish = "concrete", src = "cad", name = "Front steps"),
    column("porch.column", "porch", PORCH_POST_AT, PORCH_TOP, PORCH_BEAM[2], size = [PORCH_POST_SIZE, PORCH_POST_SIZE],
           capital = PORCH_POST_CAP, finish = "trim_white", src = "cad", name = "Pergola post"),
    beam("porch.beam", "porch", [PORCH_POST_AT[0], FRONT_Y], [PORCH_POST_AT[0], FRONT_Y - PORCH[2]], PORCH_BEAM[0],
         PORCH_BEAM[1], PORCH_BEAM[2], finish = "wood_mid", src = "cad", name = "Pergola beam"),
]
