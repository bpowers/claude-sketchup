"""Photos matched to the model: solved cameras, SketchUp renders through them, and overlays.

Modules (docs/PHOTOS.md is the workflow; conventions are written once, at the top of camera.py):

- ``paths``      where the photo tools read and write
- ``camera``     the camera model, the camera JSON, solving a camera from picks
- ``vanishing``  line segments, vanishing points, the corrected-verticals test
- ``features``   feature references: a pick's model position looked up in house.json
- ``picks``      a photo's picks file: load, validate, resolve, snap
- ``annotate``   gridded zoomed crops for reading pixel coordinates off any image
- ``solve``      command: picks -> camera JSON, render request, working overlay (with the write guard)
- ``request``    render requests, and the render cut back to the photo's geometry
- ``render``     command: render a photo's request in SketchUp, refusing a stale model
- ``compose``    command: the render's edges over the photo (working and clean overlays), the chamfer score
- ``restamp``    command: solve, render and compose photos again on the current house.json
- ``measure``    command: back-project pixels onto planes, triangulate between photos
- ``gallery``    command: the owner's review page
"""
