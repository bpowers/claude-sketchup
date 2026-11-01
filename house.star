load("lib/geometry.star", "check_unique_ids", "check_zones", "zone")
load("lib/checks.star", "all_at", "column_bears", "sills_on_floors", "stair_joins")
load("lib/frame.star", "CEILING", "EXTERIOR_WALL", "GRADE", "MAIN_FLOOR", "WALL_TOP")
load("zones/living.star", "LIVING_ELEMENTS")
load("zones/kitchen.star", "KITCHEN_ELEMENTS")
load("zones/bedroom.star", "BEDROOM_ELEMENTS")
load("zones/bath.star", "BATH_ELEMENTS")
load("zones/porch.star", "PORCH_ELEMENTS", "PORCH_TOP")
load("zones/roof.star", "ROOF_RIDGE_Z", "ROOF_ELEMENTS")

# The house as it stands today: the zones under zones/ assembled into house.json (docs/IR.md), and checked to fit
# together. A remodel is a design area's alternative (zones/areas.star, docs/DESIGN.md), never an edit here.
#
#     uv run python housec.py house.star -o house.json
#
# This is the example cottage: a single storey of about 30 x 24 ft with a living room, kitchen, bedroom and bath, a
# porch and a gable roof. Replace it zone by zone with the house being modelled.

PROJECT = {"name": "Example cottage", "address": "An example house; replace with the house being modelled"}

# Every zone an element uses, with its storey (render visibility: a camera in a room hides the zones of higher
# storeys) and whether it is outdoors (a camera there hides nothing).
ZONES = {
    "living": zone(0),
    "kitchen": zone(0),
    "bedroom": zone(0),
    "bath": zone(0),
    "porch": zone(0, outdoor = True),
    "roof": zone(9),
}

DATUMS = {"main_floor": MAIN_FLOOR, "ceiling": CEILING, "wall_top": WALL_TOP, "porch": PORCH_TOP, "grade": GRADE,
          "ridge": ROOF_RIDGE_Z}

# Where the house is, for SketchUp's geo-location and its shadows (optional; None leaves the model's own). An example.
LOCATION = {"latitude": 40.0, "longitude": -105.0, "north_angle": 0.0, "city": "Example", "country": "Example",
            "tz_offset": -7.0}

ELEMENTS = LIVING_ELEMENTS + KITCHEN_ELEMENTS + BEDROOM_ELEMENTS + BATH_ELEMENTS + PORCH_ELEMENTS + ROOF_ELEMENTS

N_IDS = check_unique_ids(ELEMENTS)
ZONES_USED = check_zones(ZONES, ELEMENTS)
ROOMS = ["living.space", "kitchen.space", "bedroom.space", "bath.space"]

# Closures across zones: each fails on a house that is wrong in a way no single zone can see.
CHECKS = [
    all_at(ELEMENTS, ROOMS, "floor_z", MAIN_FLOOR, "the main floor"),
    all_at(ELEMENTS, ROOMS, "ceiling_z", CEILING, "the ceiling"),
    # the steps start at the front door's sill, through the front wall, and land on the porch
    stair_joins(ELEMENTS, "porch.stair", "living.space", "porch.space", top_probe = EXTERIOR_WALL + 3.0),
    column_bears(ELEMENTS, "porch.column", "porch.slab", "porch.beam"),
    sills_on_floors(ELEMENTS),
]

def _document():
    doc = {
        "ir_version": 1,
        "meta": {"project": PROJECT, "variant": "today", "generated_by": "housec", "checks": CHECKS},
        "zones": ZONES,
        "datums": DATUMS,
        "elements": ELEMENTS,
    }
    if LOCATION != None:
        doc["location"] = LOCATION
    return doc

# The file's last expression is the document housec writes.
_document()
