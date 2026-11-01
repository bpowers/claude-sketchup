load("zones/areas/kitchen.star", "kitchen_layer")

# Kitchen alternative island: a freestanding island with stools on the living room side, the back run carried on to
# the refrigerator at the end wall, and the uppers taken down (zones/areas/kitchen.star holds the design).
#
# The owner: "could we try an island we can walk all the way round? And take the uppers down, so the back wall feels
# open."

{"LAYER": kitchen_layer({"id": "island", "name": "A freestanding island, the uppers taken down",
                         "block": "island", "uppers": False})}
