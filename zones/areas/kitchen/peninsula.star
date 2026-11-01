load("zones/areas/kitchen.star", "kitchen_layer")

# Kitchen alternative peninsula: a peninsula off the west wall with stools on the living room side, the back run carried
# on to the refrigerator at the end wall, and today's uppers kept (zones/areas/kitchen.star holds the design).
#
# The owner: "and the same with a peninsula off the wall instead, keeping the uppers we have."

{"LAYER": kitchen_layer({"id": "peninsula", "name": "A peninsula off the west wall, today's uppers kept",
                         "block": "peninsula", "uppers": True})}
