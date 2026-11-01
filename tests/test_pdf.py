"""extract/pdf.py and extract/pdfvec.py on small drawings made here: a room plotted with matplotlib at 1/4 in = 1 ft
(walls, a dashed line, dimension text), and a page built by hand with CAD layers, a form on a layer of its own and an
inline image. Vectors by layer, model inches through a registration, text and its dimensions, calibration, the
render cache, the point-to-pixel mapping at every page rotation, and the grid image."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import matplotlib
import numpy as np
import pypdf
import pytest
from PIL import Image
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, \
    TextStringObject

import housec
from extract import pdf as drawings
from extract import pdfvec

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PT_PER_FT = 18.0                     # 1/4 in = 1 ft: a foot is a quarter inch of paper, 18 points
OUTER = (100.0, 100.0, 460.0, 316.0)  # a 20 ft x 12 ft room's outer faces, in points
WALL = 9.0                           # a 6 in wall, in points
DASH_Y = 400.0                       # a line drawn as ten separate dashes


def _rect(x0, y0, x1, y1):
    return [x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0]


@pytest.fixture
def room_pdf(tmp_path):
    """A letter-landscape sheet: the room's outer and inner wall faces, ten dashes, and two dimension strings."""
    plt.rcParams["pdf.fonttype"] = 42             # embedded TrueType keeps the text extractable
    fig = plt.figure(figsize=(11, 8.5))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 792)
    ax.set_ylim(0, 612)
    ax.axis("off")
    x0, y0, x1, y1 = OUTER
    ax.plot(*_rect(x0, y0, x1, y1), color="k", lw=1)
    ax.plot(*_rect(x0 + WALL, y0 + WALL, x1 - WALL, y1 - WALL), color="k", lw=1)
    for k in range(10):
        ax.plot([100 + 36 * k, 124 + 36 * k], [DASH_Y, DASH_Y], color="k", lw=0.5)
    ax.text(280, 330, "20'-0\"", ha="center", fontsize=9)
    ax.text(480, 208, "12'-0\"", ha="center", fontsize=9, rotation=90)
    path = tmp_path / "room.pdf"
    fig.savefig(path)
    plt.close(fig)
    return path


@pytest.fixture
def layered_pdf(tmp_path):
    """A page built by hand as CAD exports draw one: lines under optional-content layers, a form XObject on a layer
    of its own, an inline image whose bytes read like a path, and a line on no layer."""
    w = pypdf.PdfWriter()
    page = w.add_blank_page(612, 792)

    def ocg(name):
        return w._add_object(DictionaryObject({NameObject("/Type"): NameObject("/OCG"),
                                               NameObject("/Name"): TextStringObject(name)}))

    wall, door, furn = ocg("A-WALL"), ocg("A-DOOR"), ocg("A-FURN")
    form = DecodedStreamObject()
    form.set_data(b"0 0 m 10 0 l S")
    form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                 NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (0, 0, 20, 20)]),
                 NameObject("/Matrix"): ArrayObject([NumberObject(v) for v in (1, 0, 0, 1, 300, 300)]),
                 NameObject("/OC"): furn})
    content = DecodedStreamObject()
    content.set_data(b"q 0 0 0 RG 1 w\n/OC /oc1 BDC 100 100 m 460 100 l S EMC\n"
                     b"/OC /oc2 BDC 0 0 1 RG 200 100 m 236 136 l S EMC\n"
                     b"BI /W 13 /H 1 /CS /G /BPC 8 ID 1 1 m 2 2 l S EI\n"
                     b"/Fm1 Do\n50 50 m 60 60 l S\nQ\n")
    page[NameObject("/Contents")] = w._add_object(content)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Properties"): DictionaryObject({NameObject("/oc1"): wall, NameObject("/oc2"): door}),
        NameObject("/XObject"): DictionaryObject({NameObject("/Fm1"): w._add_object(form)})})
    w._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject([wall, door, furn]),
        NameObject("/D"): DictionaryObject({NameObject("/Order"): ArrayObject([wall, door, furn])})})
    path = tmp_path / "layered.pdf"
    with open(path, "wb") as fh:
        w.write(fh)
    return path


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(drawings, "OUT", tmp_path / "out" / "pdf")
    return tmp_path / "out" / "pdf"


@pytest.fixture
def registered(tmp_path, room_pdf, monkeypatch):
    """The room sheet registered with the outer faces' south-west corner at the model's origin."""
    path = tmp_path / "registrations.json"
    path.write_text(json.dumps({"room": {"sheet": str(room_pdf), "page": 0, "origin_pt": [100.0, 100.0],
                                         "scale": 12.0 / PT_PER_FT, "rotation_deg": 0.0}}))
    monkeypatch.setattr(drawings, "REGISTRATIONS", path)
    return path


# ------------------------------------------------------------------ vectors

def test_pdfvec_tags_each_path_with_its_layer(layered_pdf):
    segs = pdfvec.extract(str(layered_pdf), 0)
    by = {}
    for s in segs:
        by.setdefault(s["layer"], []).append([tuple(round(v, 6) for v in p) for p in s["pts"]])
    assert by["A-WALL"] == [[(100.0, 100.0), (460.0, 100.0)]]
    assert by["A-DOOR"] == [[(200.0, 100.0), (236.0, 136.0)]]
    assert by["A-FURN"] == [[(300.0, 300.0), (310.0, 300.0)]]     # the form's own matrix and layer
    assert by[None] == [[(50.0, 50.0), (60.0, 60.0)]]              # the inline image's bytes drew nothing
    door = next(s for s in segs if s["layer"] == "A-DOOR")
    assert door["color"] == (0.0, 0.0, 1.0) and not door["fill"]
    assert pdfvec.layer_names(str(layered_pdf)) == ["A-WALL", "A-DOOR", "A-FURN"]


def test_info_and_layers_describe_the_sheet(layered_pdf, room_pdf, capsys):
    assert drawings.main(["info", str(layered_pdf)]) == 0
    out = capsys.readouterr().out
    assert "8.5 x 11.0 in" in out and "3 CAD layers: A-WALL, A-DOOR, A-FURN" in out
    assert drawings.main(["info", str(room_pdf)]) == 0
    assert "11.0 x 8.5 in" in capsys.readouterr().out
    assert drawings.main(["layers", str(layered_pdf)]) == 0
    out = capsys.readouterr().out
    assert [ln.split() for ln in out.splitlines() if ln.endswith("A-WALL")] == [["1", "pieces", "360", "pt", "A-WALL"]]
    counts = drawings.layer_counts(layered_pdf)
    assert counts["A-WALL"] == (1, pytest.approx(360.0)) and set(counts) == {"A-WALL", "A-DOOR", "A-FURN", None}


def test_segments_read_in_model_inches_through_a_registration(room_pdf, registered):
    reg = drawings.registration("room")
    walls = drawings.segments(room_pdf, 0, reg=reg, window=(-12, -12, 252, 156), min_length=12)
    lengths = sorted(round(s["length"], 6) for s in walls)
    assert lengths == [132.0, 132.0, 144.0, 144.0, 228.0, 228.0, 240.0, 240.0]
    south = [s for s in walls if s["axis"] == "h" and abs(s["a"][1]) < 1e-6]
    assert len(south) == 1 and sorted((south[0]["a"][0], south[0]["b"][0])) == [0.0, 240.0]
    upright = drawings.segments(room_pdf, 0, reg=reg, window=(-12, -12, 252, 156), axis="v", min_length=12)
    assert len(upright) == 4 and all(s["axis"] == "v" for s in upright)
    inside = drawings.segments(room_pdf, 0, reg=reg, window=(20, 20, 220, 124))
    assert inside == []                          # nothing crosses the room's middle


def test_merge_joins_a_dashed_line_into_one(room_pdf):
    dashes = drawings.segments(room_pdf, 0, window=(90, DASH_Y - 1, 470, DASH_Y + 1))
    assert len(dashes) == 10
    merged = drawings.merge_collinear(dashes, gap=12.0)
    assert len(merged) == 1 and merged[0]["length"] == pytest.approx(348.0)
    assert len(drawings.merge_collinear(dashes, gap=6.0)) == 10   # the 12 pt gaps stay open


def test_the_cli_judges_lengths_after_merging(room_pdf, capsys):
    """Ten 24 pt dashes are one 348 pt line: --min-length 100 keeps it when --merge joins them first."""
    assert drawings.main(["segments", str(room_pdf), "--window", "90", str(DASH_Y - 1), "470", str(DASH_Y + 1),
                          "--merge", "12", "--min-length", "100"]) == 0
    assert capsys.readouterr().out.startswith("1 pieces (pt)")


def test_a_piece_drawn_twice_is_listed_once_and_long_ones_can_be_dropped(tmp_path):
    """A frame stroked twice (on two passes, as many exports do) is one line; --max-length drops a page border."""
    w = pypdf.PdfWriter()
    page = w.add_blank_page(612, 792)
    content = DecodedStreamObject()
    content.set_data(b"q 0 0 0 RG 1 w\n18 18 m 594 18 l S\n18 18 m 594 18 l S\n100 100 m 200 100 l S\nQ\n")
    page[NameObject("/Contents")] = w._add_object(content)
    path = tmp_path / "twice.pdf"
    with open(path, "wb") as fh:
        w.write(fh)
    assert sorted(s["length"] for s in drawings.segments(path, 0)) == [100.0, 576.0]
    assert [s["length"] for s in drawings.segments(path, 0, max_length=500)] == [100.0]


def test_naming_a_layer_the_pdf_lacks_says_which_it_has(layered_pdf, room_pdf, capsys):
    assert drawings.main(["segments", str(layered_pdf), "--layer", "A-WALLS"]) == 2
    assert "no CAD layer A-WALLS; its layers are A-WALL, A-DOOR, A-FURN" in capsys.readouterr().err
    assert drawings.main(["segments", str(room_pdf), "--layer", "A-WALL"]) == 2
    assert "has no CAD layers, so there is no layer A-WALL" in capsys.readouterr().err
    # the room's long wall faces, without the page's background frame
    assert drawings.main(["segments", str(room_pdf), "--layer", "None", "--min-length", "300", "--max-length",
                          "400"]) == 0
    assert capsys.readouterr().out.startswith("4 pieces (pt)")


def test_the_cli_lists_segments_and_honours_the_limit(room_pdf, registered, capsys):
    assert drawings.main(["segments", str(room_pdf), "--registration", "room", "--min-length", "100",
                          "--axis", "h", "--window", "-12", "-12", "252", "156", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("4 pieces (in, through room)") and "... 2 more" in out


# ------------------------------------------------------------------ text and dimensions

@pytest.mark.parametrize("text, inches", [
    ("12'-6\"", 150.0), ("12'-6 1/2\"", 150.5), ("12' - 6 1/2\"", 150.5), ("12'-6-1/2\"", 150.5),
    ("3'-0\"", 36.0), ("12'", 144.0), ("7 1/2\"", 7.5), ("1/2\"", 0.5), ("12\u2019-6\u201d", 150.0), ("12\u2032-6\u2033", 150.0),
    ("150.5", 150.5), ("6\"", 6.0), ("0'-3 3/4\"", 3.75),
])
def test_parse_dim_reads_feet_and_inches(text, inches):
    assert drawings.parse_dim(text) == pytest.approx(inches)


@pytest.mark.parametrize("text", ["twelve feet", "12'-6\" and 3'", "", "N.T.S."])
def test_parse_dim_refuses_what_is_no_single_dimension(text):
    with pytest.raises(drawings.DrawingError):
        drawings.parse_dim(text)


def test_find_dims_and_ftin():
    assert drawings.find_dims("BEDROOM 2  11'-6\" x 10'-0\"  CLG 8'") == [138.0, 120.0, 96.0]
    assert drawings.find_dims("2X6 @ 16 O.C.") == []
    assert drawings.ftin(150.5) == "12'-6 1/2\"" and drawings.ftin(36) == "3'-0\"" and drawings.ftin(-3.75) == \
        "-0'-3 3/4\""


def test_text_runs_carry_their_positions_and_dimensions(room_pdf, registered, capsys):
    runs = {r["text"]: r for r in drawings.text_runs(room_pdf, 0)}
    assert set(runs) == {"20'-0\"", "12'-0\""}
    across = runs["20'-0\""]
    assert across["at"][0] == pytest.approx(280.0, abs=2.0) and 330.0 <= across["at"][1] <= 340.0
    upright = runs["12'-0\""]
    assert upright["box"][3] - upright["box"][1] > upright["box"][2] - upright["box"][0]    # set sideways
    assert drawings.main(["text", str(room_pdf), "--registration", "room", "--match", "^20"]) == 0
    [row] = capsys.readouterr().out.splitlines()[1:]
    model = [float(v) for v in row.split("model")[1].split()[:2]]
    assert row.endswith("= 240 in") and model == pytest.approx([120.0, 155.0], abs=2.0)


# Adobe's widths of Helvetica's characters, in thousandths of the font size: what pdfium spaces them by.
HELVETICA = {**{d: 556 for d in "0123456789"}, '"': 355, "'": 191, "/": 278, ",": 278, ":": 278, "=": 584, "-": 333,
             "A": 667, "C": 722, "E": 667, "L": 556, "N": 722, "P": 667, "R": 722, "S": 667, "T": 611, "V": 667,
             "Y": 667, " ": 278}


def _letter_by_letter_pdf(path, strings, size=10.0):
    """A page whose every character is its own text object, as CAD plotters write them. strings: (text, x, y,
    turned, times): the text from (x, y) left to right, or bottom to top when turned, drawn `times` over itself."""
    ops = []
    for text, x, y, turned, times in strings:
        for _ in range(times):
            at = 0.0
            for ch in text:
                if ch != " ":
                    m = f"0 1 -1 0 {x:.3f} {y + at:.3f}" if turned else f"1 0 0 1 {x + at:.3f} {y:.3f}"
                    ops.append(f"BT /F1 {size:g} Tf {m} Tm ({ch}) Tj ET")
                at += HELVETICA[ch] * size / 1000.0
    content = "\n".join(ops).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
            b"/Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def test_text_runs_join_a_plotters_letters_by_where_they_stand(tmp_path):
    """Spaced characters join into one string with its spaces; two strings of a dimension chain 6 pt apart stay two;
    a label's wide spaces stay inside it; a label printed three times over itself reads once; turned text reads along
    its own direction."""
    gap = 6.0 + HELVETICA["1"] * 0.01 + HELVETICA['"'] * 0.01         # `1"` and `20"` 6 pt apart
    pdf = _letter_by_letter_pdf(tmp_path / "letters.pdf", [
        ('64 1/2"', 100, 100, False, 1),
        ('1"', 200, 100, False, 1), ('20"', 200 + gap, 100, False, 1),
        ("PANTRY", 100, 200, False, 3),
        ("SCALE: 1/2\"   =   1'-0\"", 100, 300, False, 1),
        ('5/8", REVEAL', 400, 100, True, 1),
    ])
    runs = {r["text"]: r for r in drawings.text_runs(pdf, 0)}
    assert set(runs) == {'64 1/2"', '1"', '20"', "PANTRY", "SCALE: 1/2\" = 1'-0\"", '5/8", REVEAL'}
    assert drawings.find_dims('64 1/2"') == [64.5]
    assert runs['5/8", REVEAL']["angle"] == 90 and {r["angle"] for t, r in runs.items() if "REVEAL" not in t} == {0}
    turned = runs['5/8", REVEAL']["box"]
    assert turned[3] - turned[1] > 5 * (turned[2] - turned[0])                    # it stands upright on the page


# ------------------------------------------------------------------ scale and registration

def test_calibrate_gives_the_scale_checks_it_and_registers_the_sheet(room_pdf, registered, capsys):
    registered.unlink()
    x0, y0, x1, y1 = OUTER
    assert drawings.main(["calibrate", str(room_pdf), "--a", str(x0), str(y0), "--b", str(x1), str(y0),
                          "--real", "20'-0\"", "--check", str(x1), str(y0), str(x1), str(y1), "12'",
                          "--check", str(x0), str(y0), str(x0), str(y1), "11'-10\"",
                          "--a-model", "0", "0", "--register", "room"]) == 0
    out = capsys.readouterr().out
    assert "0.666667 in per pt" in out and "nearest standard scale: 1/4 in = 1 ft" in out
    assert "measures 144.00 in, off +0.00 in" in out and "off +2.00 in" in out and "worst check: 2.00 in" in out
    assert "ROOM = registration([100, 100], 0.66666667)" in out          # the line that mirrors it in lib/frame.star
    saved = json.loads(registered.read_text())["room"]
    assert saved["origin_pt"] == [100.0, 100.0] and saved["scale"] == pytest.approx(2.0 / 3.0)
    assert saved["page"] == 0 and saved["rotation_deg"] == 0.0


def test_calibrate_reads_pixels_of_a_render(room_pdf, capsys):
    # at 72 dpi a pixel is a point, rows counted down from the top of the 612 pt page
    assert drawings.main(["calibrate", str(room_pdf), "--units", "px", "--dpi", "72", "--a", "100", "512",
                          "--b", "460", "512", "--real", "240"]) == 0
    assert "0.666667 in per pt" in capsys.readouterr().out


def test_nearest_scale_names_the_architects_scale_and_the_drift():
    assert drawings.nearest_scale(2.0 / 3.0)[0] == "1/4 in = 1 ft"
    name, nominal, off = drawings.nearest_scale(0.65)
    assert name == "1/4 in = 1 ft" and off == pytest.approx(-0.025)
    assert drawings.nearest_scale(96 / 72.0)[0] == "1/8 in = 1 ft"


@pytest.mark.parametrize("rotation", [0.0, 30.0, 90.0, -135.0])
def test_a_registration_maps_both_ways_and_solves_from_one_point(rotation):
    origin = drawings.solve_registration((250.0, 120.0), (36.0, 48.0), 0.5, rotation)
    reg = drawings.Registration("r", drawings.ROOT / "x.pdf", 0, origin, 0.5, rotation)
    assert reg.to_model(250.0, 120.0) == pytest.approx((36.0, 48.0))
    for p in [(0.0, 0.0), (100.0, -40.0), (12.5, 300.0)]:
        assert reg.to_model(*reg.to_pt(*p)) == pytest.approx(p)
    a, b = reg.to_model(10.0, 10.0), reg.to_model(10.0 + 72.0, 10.0)
    assert math.dist(a, b) == pytest.approx(36.0)
    assert math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) == pytest.approx(rotation)


def test_an_unknown_registration_says_which_there_are(registered):
    with pytest.raises(drawings.DrawingError, match="no registration 'plan' .* it has room"):
        drawings.registration("plan")


# ------------------------------------------------------------------ rasters

def test_render_is_cached_by_content_page_and_dpi(room_pdf, out_dir):
    a = drawings.render(room_pdf, 0, 72)
    stamp = a.stat().st_mtime_ns
    assert drawings.render(room_pdf, 0, 72) == a and a.stat().st_mtime_ns == stamp
    b = drawings.render(room_pdf, 0, 144)
    assert b != a and Image.open(b).size == (1584, 1224)
    assert a.is_relative_to(out_dir)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_points_land_on_their_pixels_at_every_page_rotation(room_pdf, out_dir, tmp_path, rotation):
    reader = pypdf.PdfReader(room_pdf)
    w = pypdf.PdfWriter()
    w.add_page(reader.pages[0])
    w.pages[0][NameObject("/Rotate")] = NumberObject(rotation)
    path = tmp_path / f"rot{rotation}.pdf"
    with open(path, "wb") as fh:
        w.write(fh)
    geom = drawings.page(path, 0)
    assert geom.rotation == rotation
    img = np.asarray(drawings.open_render(path, 0, 72).convert("L"))
    for pt in [(100.0, 100.0), (460.0, 316.0), (280.0, 100.0)]:          # wall corners and the middle of a wall
        px, py = geom.to_px(*pt, 72)
        assert img[int(py) - 2:int(py) + 3, int(px) - 2:int(px) + 3].min() < 200, (rotation, pt)
        assert geom.to_pt(px, py, 72) == pytest.approx(pt)
    assert img[int(geom.to_px(280.0, 208.0, 72)[1]), int(geom.to_px(280.0, 208.0, 72)[0])] > 200   # mid-room


def test_grid_crops_zooms_and_marks(room_pdf, out_dir, capsys):
    assert drawings.main(["grid", str(room_pdf), "--center", "100", "100", "--size", "80", "--dpi", "144",
                          "--zoom", "2", "--mark", "100", "100"]) == 0
    path = drawings.ROOT / capsys.readouterr().out.strip()
    if not path.is_file():
        path = next(out_dir.rglob("*.png"))
    img = np.asarray(Image.open(path).convert("RGB")).astype(int)
    assert img.shape[0] == pytest.approx(80 * 2 * 2, abs=4) and img.shape[1] == pytest.approx(80 * 2 * 2, abs=4)
    red = (img[..., 0] > 180) & (img[..., 1] < 80) & (img[..., 2] < 80)
    ys, xs = np.nonzero(red)
    assert abs(np.median(xs) - 160) < 12 and abs(np.median(ys) - 160) < 12     # the mark at the crop's centre
    with pytest.raises(drawings.DrawingError, match="outside page 0"):
        drawings.grid_image(room_pdf, 0, (5000, 5000, 5100, 5100), dpi=72)


# ------------------------------------------------------------------ the house's registrations

def test_lib_frame_mirrors_its_registrations_exactly():
    """Each registration(...) constant in lib/frame.star mirrors the registration of the same name, lower-cased, in
    sources/plans/registrations.json (PLAN_MAIN is plan_main): Starlark cannot read the file, so the two copies are
    held together here."""
    frame = ROOT / "lib" / "frame.star"
    names = re.findall(r"^([A-Z][A-Z0-9_]*)\s*=\s*registration\(", frame.read_text(), re.M)
    regs = drawings.registrations()
    for name in names:
        assert name.lower() in regs, (f"lib/frame.star's {name} mirrors no registration {name.lower()} in "
                                      f"sources/plans/registrations.json (it has {', '.join(regs) or 'none'})")
        got = housec._eval(housec.inline(frame, ROOT) + f"\n{name}\n", "frame.star")
        want = regs[name.lower()]
        assert got["origin_pt"] == pytest.approx(want["origin_pt"], abs=1e-3), name
        assert got["scale"] == pytest.approx(want["scale"], rel=1e-6), name
        assert got["rotation_deg"] == pytest.approx(want.get("rotation_deg", 0.0), abs=1e-6), name
