"""Gridded, zoomed crops of an image, for reading pixel coordinates off it.

    uv run python -m overlay.annotate IMAGE                      overview and tiles
    uv run python -m overlay.annotate IMAGE --at 640,290 --at 870,230

IMAGE is a photo stem (sources/photos/<stem>.jpg) or any image path: a photo, a rasterised drawing, a render. Output
goes to out/annotate/<stem>/:

    overview.png            the whole image, a grid every 50 px, labels every 100 px in the margins
    tile_u0000_v0000.png    256 px tiles overlapping by 48 px, at 3x, a grid every 10 px, labels every 50 px
    at_u0640_v0290.png      a 48 px radius round the point at 8x, a grid every 2 px, labels every 10 px, a crosshair
    index.json              which pixel range each file covers

Grid lines sit on pixel corners: the line labelled 640 is the left edge of pixel column 640, so a feature centred in
that column reads 640.5 (camera.py's convention). Reading a pick: find the feature on the overview, read it to a few
pixels on its tile, then confirm with --at, whose crosshair shows exactly where the numbers you wrote land.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw

from . import paths
from .compose import font

MARGIN = 44
BG = (28, 28, 30)
MINOR = (255, 255, 255, 60)
MAJOR = (255, 220, 0, 170)
LABEL = (255, 220, 0)
CROSS = (0, 255, 255)


def render_crop(img: np.ndarray, u0: int, v0: int, u1: int, v1: int, *, zoom: int, minor: int, major: int,
                crosshair: Sequence[float] | None = None, title: str | None = None) -> Image.Image:
    """Crop [u0, u1) x [v0, v1) (padded grey past the image), enlarge with nearest-neighbour so pixel boundaries stay
    visible, and draw a labelled grid."""
    h, w = img.shape[:2]
    cw, ch = u1 - u0, v1 - v0
    crop = np.full((ch, cw, 3), 90, dtype=np.uint8)
    su0, sv0, su1, sv1 = max(u0, 0), max(v0, 0), min(u1, w), min(v1, h)
    if su1 > su0 and sv1 > sv0:
        crop[sv0 - v0:sv1 - v0, su0 - u0:su1 - u0] = img[sv0:sv1, su0:su1]
    top = MARGIN + (22 if title else 0)
    canvas = Image.new("RGB", (cw * zoom + 2 * MARGIN, ch * zoom + top + MARGIN), BG)
    canvas.paste(Image.fromarray(crop).resize((cw * zoom, ch * zoom), Image.NEAREST), (MARGIN, top))
    over = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for u in range(int(math.ceil(u0 / minor)) * minor, u1 + 1, minor):
        x = MARGIN + (u - u0) * zoom
        d.line([(x, top), (x, top + ch * zoom)], fill=MAJOR if u % major == 0 else MINOR,
               width=2 if u % major == 0 else 1)
    for v in range(int(math.ceil(v0 / minor)) * minor, v1 + 1, minor):
        y = top + (v - v0) * zoom
        d.line([(MARGIN, y), (MARGIN + cw * zoom, y)], fill=MAJOR if v % major == 0 else MINOR,
               width=2 if v % major == 0 else 1)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), over).convert("RGB")
    d = ImageDraw.Draw(canvas)
    fnt = font(14)
    for u in range(int(math.ceil(u0 / major)) * major, u1 + 1, major):
        x, s = MARGIN + (u - u0) * zoom, str(u)
        tw = d.textlength(s, font=fnt)
        d.text((x - tw / 2, top - 20), s, fill=LABEL, font=fnt)
        d.text((x - tw / 2, top + ch * zoom + 4), s, fill=LABEL, font=fnt)
    for v in range(int(math.ceil(v0 / major)) * major, v1 + 1, major):
        y, s = top + (v - v0) * zoom, str(v)
        d.text((MARGIN - d.textlength(s, font=fnt) - 4, y - 7), s, fill=LABEL, font=fnt)
        d.text((MARGIN + cw * zoom + 4, y - 7), s, fill=LABEL, font=fnt)
    if title:
        d.text((MARGIN, 4), title, fill=(230, 230, 230), font=fnt)
    if crosshair is not None:
        x, y, r = MARGIN + (crosshair[0] - u0) * zoom, top + (crosshair[1] - v0) * zoom, 3 * zoom
        d.ellipse([x - r, y - r, x + r, y + r], outline=CROSS, width=2)
        for a, b in (((x - 2 * r, y), (x - r, y)), ((x + r, y), (x + 2 * r, y)), ((x, y - 2 * r), (x, y - r)),
                     ((x, y + r), (x, y + 2 * r))):
            d.line([a, b], fill=CROSS, width=2)
    return canvas


def tile_origins(total: int, tile: int, overlap: int) -> list[int]:
    if total <= tile:
        return [0]
    return sorted(set(list(range(0, total - tile, tile - overlap)) + [total - tile]))


def resolve_image(arg: str | Path) -> Path:
    """A photo stem or any image path."""
    p = Path(str(arg))
    if p.is_file():
        return p.resolve()
    if (paths.ROOT / p).is_file():
        return (paths.ROOT / p).resolve()
    return paths.resolve_photo(arg)


def annotate(image: str | Path, *, out_dir: Path | None = None, tiles: bool = True, tile: int = 256,
             overlap: int = 48, zoom: int = 3, at: Sequence[Sequence[float]] = (), at_radius: int = 48,
             at_zoom: int = 8) -> dict:
    path = resolve_image(image)
    stem = path.stem
    img = paths.load_rgb(path)
    h, w = img.shape[:2]
    out_dir = Path(out_dir or paths.ANNOTATE / stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    index: dict = {"image": str(path), "image_size": [w, h], "files": []}

    def save(im: Image.Image, name: str, box: Sequence[int], **extra) -> None:
        im.save(out_dir / name)
        index["files"].append({"file": name, "box_u0_v0_u1_v1": list(box), **extra})

    if not at:
        save(render_crop(img, 0, 0, w, h, zoom=1, minor=50, major=100, title=f"{stem}  {w}x{h}"), "overview.png",
             (0, 0, w, h), zoom=1)
    if tiles and not at:
        for v0 in tile_origins(h, tile, overlap):
            for u0 in tile_origins(w, tile, overlap):
                box = (u0, v0, min(u0 + tile, w), min(v0 + tile, h))
                save(render_crop(img, *box, zoom=zoom, minor=10, major=50,
                                 title=f"{stem}  u {box[0]}..{box[2]}  v {box[1]}..{box[3]}  x{zoom}"),
                     f"tile_u{u0:04d}_v{v0:04d}.png", box, zoom=zoom)
    for p in at:
        u, v = float(p[0]), float(p[1])
        box = (int(math.floor(u)) - at_radius, int(math.floor(v)) - at_radius,
               int(math.floor(u)) + at_radius, int(math.floor(v)) + at_radius)
        save(render_crop(img, *box, zoom=at_zoom, minor=2, major=10, crosshair=(u, v),
                         title=f"{stem}  point ({u:g}, {v:g})  x{at_zoom}"),
             f"at_u{int(u):04d}_v{int(v):04d}.png", box, zoom=at_zoom, point=[u, v])
    (out_dir / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    index["out_dir"] = str(out_dir)
    return index


def _pair(s: str) -> tuple[float, float]:
    parts = s.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected u,v, got {s!r}")
    return float(parts[0]), float(parts[1])


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m overlay.annotate",
                                 description="gridded zoomed crops for reading pixel coordinates")
    ap.add_argument("image", help="a photo stem or an image path")
    ap.add_argument("--out", type=Path, help="output folder (default out/annotate/<stem>)")
    ap.add_argument("--no-tiles", action="store_true", help="the overview only")
    ap.add_argument("--tile", type=int, default=256)
    ap.add_argument("--overlap", type=int, default=48)
    ap.add_argument("--zoom", type=int, default=3)
    ap.add_argument("--at", type=_pair, action="append", default=[], help="u,v: a zoomed crop round a point")
    ap.add_argument("--at-radius", type=int, default=48)
    ap.add_argument("--at-zoom", type=int, default=8)
    a = ap.parse_args(argv)
    try:
        index = annotate(a.image, out_dir=a.out, tiles=not a.no_tiles, tile=a.tile, overlap=a.overlap, zoom=a.zoom,
                         at=a.at, at_radius=a.at_radius, at_zoom=a.at_zoom)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 2
    print(f"wrote {len(index['files'])} files to {paths.to_root(index['out_dir'])}")
    for f in index["files"]:
        print(f"  {f['file']}  u0,v0,u1,v1 = {f['box_u0_v0_u1_v1']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
