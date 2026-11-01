"""Where the photo tools read and write, and photo lookup.

Everything is derived from this file's location, so the repository works wherever it is checked out. Files the
overlay commits record paths relative to the repository root (``to_root``) and read them back with ``from_root``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

OVERLAY = Path(__file__).resolve().parent
ROOT = OVERLAY.parent
HOUSE = ROOT / "house.json"
PHOTOS = ROOT / "sources" / "photos"

PICKS = OVERLAY / "picks"
CAMERAS = OVERLAY / "cameras"
REQUESTS = OVERLAY / "requests"

OUT = ROOT / "out"
RENDERS = OUT / "renders"
OVERLAYS = OUT / "overlays"
ANNOTATE = OUT / "annotate"

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")
# The Ruby that draws a render: an edit to either draws differently, so a render older than both is out of date.
BUILDERS = (ROOT / "build" / "build.rb", ROOT / "build" / "render.rb")


def to_root(path: str | Path) -> str:
    """A path as a committed file records it: relative to the repository root when it lies inside, else absolute."""
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def from_root(path: str | Path) -> Path:
    """A path read from a committed file: a relative one lies under the repository root."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def photo_stem(arg: str | Path) -> str:
    """'sources/photos/living-01.jpg', 'living-01.jpg' and 'living-01' all give 'living-01'."""
    return Path(str(arg)).stem


def resolve_photo(arg: str | Path) -> Path:
    """A photo from a path (relative to the working directory or the root) or a bare stem in sources/photos.

    Raises FileNotFoundError listing every place tried.
    """
    p = Path(str(arg))
    candidates = [p, ROOT / p, PHOTOS / p.name]
    if not p.suffix:
        candidates += [PHOTOS / (p.name + s) for s in PHOTO_SUFFIXES]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise FileNotFoundError("photo not found; tried: " + ", ".join(str(c) for c in candidates))


def load_rgb(path: str | Path) -> np.ndarray:
    """An image as an H x W x 3 uint8 RGB array, EXIF orientation applied (phones store photos rotated)."""
    with Image.open(path) as im:
        return np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
