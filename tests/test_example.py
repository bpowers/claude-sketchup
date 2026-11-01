"""The example cottage's worked example of the drawing and photo tools (tests/fixtures/example_sources.py draws it):
its plan and elevation sheets register onto the cottage line for line, lib/frame.star's PLAN_MAIN is the plan's
registration, and its photo's picks solve a camera. Skipped once house.json is another house or the example's files
are gone.

    uv run pytest tests/test_example.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from extract import pdf as drawings
from tools import plancheck

ROOT = Path(__file__).resolve().parent.parent
HOUSE = ROOT / "house.json"
SHEET = ROOT / "sources" / "plans" / "example-cottage.pdf"
PHOTO = ROOT / "sources" / "photos" / "living-01.jpg"


def _cottage():
    try:
        return json.loads(HOUSE.read_text())["meta"]["project"]["name"] == "Example cottage"
    except (OSError, KeyError, ValueError):
        return False


pytestmark = pytest.mark.skipif(not (_cottage() and SHEET.is_file() and PHOTO.is_file()),
                                reason="the example cottage's sources; house.json is another house, or they are gone")


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(drawings, "OUT", tmp_path / "pdf")
    return tmp_path


def test_the_plan_sheet_draws_every_wall_face_where_the_cottage_has_it(out):
    lines = []
    path, rows = plancheck.over_sheet("plan_main", layers=["A-WALL"], out=out / "plan.png", echo=lines.append)
    assert rows and all(r["offset"] == pytest.approx(0.0, abs=0.01) for r in rows), lines
    assert path.is_file()


def test_the_elevation_sheet_draws_every_height_where_the_cottage_has_it(out):
    lines = []
    _, rows = plancheck.over_sheet("elev_front", facing="-y", layers=["A-ELEV"], out=out / "elev.png",
                                   echo=lines.append)
    got = {(r["id"], r["face"]): r["offset"] for r in rows}
    assert all(v == pytest.approx(0.0, abs=0.01) for v in got.values()), lines
    assert ("living.wall.front.window", "sill") in got and ("roof.plane.front", "underside") in got


def test_the_plan_sheets_scale_measures_on_its_dimension_string(capsys):
    # the front wall's 30'-0" overall, from the west face's outer corner to the east's, and the 24'-0" across
    assert drawings.main(["calibrate", str(SHEET), "--page", "0", "--a", "300", "220", "--b", "840", "220", "--real",
                          "30'-0\"", "--check", "300", "220", "300", "652", "24'-0\""]) == 0
    out = capsys.readouterr().out
    assert "measured +0.00% from it" in out and "off +0.00 in" in out


def test_every_pick_stands_on_an_edge_the_photo_shows():
    """The picks are where their features project through the camera that drew the photo; each must also be where
    the photo shows that feature, an edge, or the photo misleads anyone who reads it (a face drawn where a nearer one
    hides it, say)."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    gray = ndimage.gaussian_filter(np.asarray(Image.open(PHOTO).convert("L"), float), 1.0)
    strength = np.hypot(ndimage.sobel(gray, 1), ndimage.sobel(gray, 0)) / 8.0
    flat = []
    for p in json.loads((ROOT / "overlay" / "picks" / "living-01.json").read_text())["points"]:
        j, i = int(p["px"][0]), int(p["px"][1])
        if strength[i - 3:i + 4, j - 3:j + 4].max() < 4.0:      # plain wall or floor is under 1
            flat.append(p["label"])
    assert not flat, f"no edge in the photo within 3 px of: {flat}"


def test_the_photos_picks_solve_its_camera():
    from overlay.features import House
    from overlay.solve import Options, solve_photo
    sol = solve_photo("living-01", Options(), house=House.load(HOUSE))
    assert not sol.guard["refused"] and sol.doc["residual_px"]["rms"] < 1.5
    assert sol.doc["check_residual_px"]["rms"] < 2.0
