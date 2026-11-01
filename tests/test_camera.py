"""The camera model, the lens, solving from synthetic picks, and the write guard. No SketchUp, no files."""
import json
import math

import numpy as np
import pytest

from overlay.camera import (Camera, CameraError, Correspondence, dlt_camera, euler_from_rotation, look_at_rotation,
                            radial_distort, radial_fold_radius, radial_undistort, residual_vectors,
                            rotation_from_euler, solve_camera)
from overlay.solve import write_guard

W, H = 1280, 853
# a room-sized box in front of the camera, inches
ROOM = dict(x0=0.0, x1=240.0, y0=0.0, y1=200.0, z0=0.0, z1=96.0)


def room_points() -> np.ndarray:
    """Points over the far wall (corners, jambs, sills, heads), both side walls and the floor and ceiling lines."""
    r = ROOM
    pts = [(x, r["y1"], z) for x in (r["x0"], 60.0, 108.0, 170.0, r["x1"]) for z in (r["z0"], 36.0, 84.0, r["z1"])]
    for y in (90.0, 130.0, 170.0):
        pts += [(r["x0"], y, 36.0), (r["x1"], y, 36.0), (r["x0"], y, r["z1"]), (r["x1"], y, r["z1"])]
    return np.array(pts, dtype=float)


def camera(**kw) -> Camera:
    """A camera standing near the room's open end, looking at the far wall."""
    d = dict(f=640.0 / math.tan(math.radians(80.0) / 2.0), eye=(120.0, -30.0, 58.0), yaw=91.5, pitch=-4.0, roll=0.7,
             principal_offset=(0.0, 0.0), pixel_aspect=1.0, distortion=(0.0, 0.0))
    d.update(kw)
    return Camera.from_euler((W, H), d["f"], d["eye"], d["yaw"], d["pitch"], d["roll"], d["principal_offset"],
                             d["pixel_aspect"], d["distortion"])


def picks(cam: Camera, X: np.ndarray, noise_px: float = 0.0, sigma: float = 1.0, seed: int = 0) -> list:
    uv = cam.project(X)
    if noise_px:
        uv = uv + np.random.default_rng(seed).normal(0.0, noise_px, uv.shape)
    return [Correspondence(f"p{i}", tuple(p), tuple(x), sigma_px=sigma) for i, (p, x) in enumerate(zip(uv, X))]


def errors(sol, truth: Camera) -> dict:
    return dict(f=sol.camera.f - truth.f, eye=sol.camera.eye - truth.eye,
                offset=np.subtract(sol.camera.principal_offset, truth.principal_offset),
                euler=np.subtract(sol.camera.euler_deg(), truth.euler_deg()))


# ------------------------------------------------------------------ conventions


def test_projection_conventions():
    cam = Camera.from_euler((W, H), 900.0, (300.0, 100.0, 60.0), yaw_deg=90.0)
    # the image centre is (W/2, H/2) with pixel corners on integers
    np.testing.assert_allclose(cam.project([300.0, 400.0, 60.0]), [640.0, 426.5], atol=1e-9)
    # looking along +y: +x is to the right, +z up the image
    assert cam.project([310.0, 400.0, 60.0])[0] > 640.0
    assert cam.project([300.0, 400.0, 70.0])[1] < 426.5
    np.testing.assert_allclose(cam.project([310.0, 400.0, 60.0])[0] - 640.0, 900.0 * 10 / 300, atol=1e-9)
    shifted = cam.replace(principal_offset=(12.0, -40.0))
    np.testing.assert_allclose(shifted.project([300.0, 400.0, 60.0]), [652.0, 386.5], atol=1e-9)
    # the pixel aspect stretches v about the principal point only
    tall = cam.replace(pixel_aspect=1.05)
    np.testing.assert_allclose(tall.project([300.0, 400.0, 70.0])[1] - 426.5,
                               1.05 * (cam.project([300.0, 400.0, 70.0])[1] - 426.5), atol=1e-9)


def test_euler_round_trip_and_signs():
    rng = np.random.default_rng(1)
    for _ in range(100):
        yaw, pitch, roll = rng.uniform(-179, 179), rng.uniform(-85, 85), rng.uniform(-60, 60)
        R = rotation_from_euler(yaw, pitch, roll)
        np.testing.assert_allclose(R @ R.T, np.eye(3), atol=1e-12)
        np.testing.assert_allclose(euler_from_rotation(R), [yaw, pitch, roll], atol=1e-9)
    np.testing.assert_allclose(rotation_from_euler(90.0, 0.0, 0.0), look_at_rotation([0, 0, 0], [0, 1, 0], [0, 0, 1]),
                               atol=1e-12)
    assert Camera.from_euler((W, H), 800.0, (0, 0, 0), 90.0, pitch_deg=10.0).forward[2] > 0      # pitch up looks up
    assert Camera.from_euler((W, H), 800.0, (0, 0, 0), 90.0, roll_deg=10.0).right[2] > 0         # roll lifts the right


def test_camera_json_round_trip_with_aspect_and_lens(tmp_path):
    cam = camera(principal_offset=(-6.25, -58.5), pixel_aspect=1.03, distortion=(-0.06, 0.012))
    doc = json.loads(json.dumps(cam.to_json(photo="living-01.jpg", target_distance=150.0)))
    assert doc["distortion"] == {"model": "radial2", "k1": pytest.approx(-0.06), "k2": pytest.approx(0.012)}
    back = Camera.from_json(doc)
    X = np.random.default_rng(2).uniform([0, 100, 0], [240, 200, 96], (50, 3))
    np.testing.assert_allclose(back.project(X), cam.project(X), atol=1e-5)
    fwd = np.subtract(doc["target"], doc["eye"])
    assert abs(np.dot(doc["up"], fwd / np.linalg.norm(fwd))) < 1e-7      # up is written exactly
    assert "distortion" not in cam.pinhole().to_json()
    cam.save(tmp_path / "c.json")
    np.testing.assert_allclose(Camera.load(tmp_path / "c.json").project(X), cam.project(X), atol=1e-5)
    with pytest.raises(CameraError, match="radial2"):
        Camera.from_json(dict(doc, distortion={"model": "fisheye", "k1": 0.1}))
    with pytest.raises(CameraError, match="fov_v_deg"):
        Camera.from_json({"image_size": [W, H], "eye": [0, 0, 0], "target": [0, 1, 0]})


# ------------------------------------------------------------------------ lens


def test_radial_map_inverts_and_folds():
    for k1, k2 in ((-0.06, 0.012), (0.08, -0.01), (-0.12, 0.0), (0.0, 0.05)):
        r = np.linspace(0.0, 1.3, 400)
        r = r[r < 0.97 * radial_fold_radius(k1, k2)]
        np.testing.assert_allclose(radial_undistort(radial_distort(r, k1, k2), k1, k2), r, atol=1e-11)
    assert radial_fold_radius(-0.12, 0.0) == pytest.approx(math.sqrt(1.0 / 0.36))
    assert radial_fold_radius(0.05, 0.01) == math.inf
    assert camera(distortion=(-0.06, 0.012)).lens_problem() is None
    assert "folds over" in camera(f=500.0, distortion=(-0.3, 0.0)).lens_problem()


def test_projection_rays_and_pixel_maps_agree_under_a_lens():
    cam = camera(principal_offset=(12.0, -55.0), pixel_aspect=1.03, distortion=(-0.07, 0.015))
    X = room_points()
    uv = cam.project(X)
    np.testing.assert_allclose(cam.distort_px(cam.pinhole().project(X)), uv, atol=1e-9)
    np.testing.assert_allclose(cam.undistort_px(uv), cam.pinhole().project(X), atol=1e-8)
    d = X - cam.eye
    np.testing.assert_allclose(cam.ray(uv), d / np.linalg.norm(d, axis=1, keepdims=True), atol=1e-10)
    # a line pick's residual is zero on the projected curve
    line = Correspondence("edge", tuple(uv[3]), (X[3][0], X[3][1], None))
    vec, _ = residual_vectors([line], cam)
    assert np.linalg.norm(vec[0]) < 1e-6


# ----------------------------------------------------------------------- solve


def test_dlt_recovers_an_exact_camera():
    truth = camera(principal_offset=(22.0, -61.0))
    X = room_points()
    cam, why = dlt_camera(X, truth.project(X), (W, H))
    assert cam is not None, why
    assert cam.f == pytest.approx(truth.f, rel=1e-6)
    np.testing.assert_allclose(cam.eye, truth.eye, atol=1e-4)
    np.testing.assert_allclose(cam.principal_offset, truth.principal_offset, atol=1e-3)


def test_solve_noise_free():
    truth = camera()
    sol = solve_camera(picks(truth, room_points()), (W, H))
    e = errors(sol, truth)
    assert sol.rms_px < 1e-3 and abs(e["f"]) < 1e-2
    np.testing.assert_allclose(e["eye"], 0, atol=1e-2)
    np.testing.assert_allclose(e["euler"], 0, atol=1e-3)


@pytest.mark.parametrize("attitude", ["free", "zero_pitch", "level"])
def test_solve_level_camera_with_shifted_principal_point(attitude):
    # the listing-photo case: verticals corrected, so the camera is level and the recomposition shifted the frame
    truth = camera(pitch=0.0, roll=0.0, principal_offset=(8.0, -62.0))
    sol = solve_camera(picks(truth, room_points()), (W, H), attitude=attitude)
    e = errors(sol, truth)
    assert sol.rms_px < 1e-3
    np.testing.assert_allclose(e["offset"], 0, atol=0.05)
    np.testing.assert_allclose(e["eye"], 0, atol=0.02)


def test_solve_recovers_aspect_and_lens_from_noisy_picks():
    truth = camera(pitch=0.0, roll=0.0, principal_offset=(4.0, -50.0), pixel_aspect=1.04, distortion=(-0.05, 0.0))
    sol = solve_camera(picks(truth, room_points(), noise_px=0.5, sigma=0.5, seed=3), (W, H), attitude="level",
                       aspect="free", distortion="k1")
    cam = sol.camera
    assert sol.rms_px < 1.0
    assert cam.pixel_aspect == pytest.approx(1.04, abs=4 * sol.std["pixel_aspect"] + 1e-3)
    assert cam.k1 == pytest.approx(-0.05, abs=4 * sol.std["k1"] + 1e-3)
    assert np.linalg.norm(cam.eye - truth.eye) < 2.0
    assert sol.lens["k1"]["prior_sigma"] == 0.15
    # a square-pixel pinhole solve of the same picks cannot fit them
    assert solve_camera(picks(truth, room_points(), noise_px=0.5, sigma=0.5, seed=3), (W, H),
                        attitude="level").rms_px > 3 * sol.rms_px


def test_line_picks_hold_the_camera_with_few_points():
    truth = camera(principal_offset=(5.0, -30.0))
    X = room_points()
    corrs = picks(truth, X[[0, 3, 16, 19, 25]])                       # five full points
    lines = [((60.0, 200.0, 55.0), 2), ((170.0, 200.0, 12.0), 2), ((0.0, 110.0, 96.0), 1), ((240.0, 150.0, 96.0), 1),
             ((90.0, 200.0, 96.0), 0), ((0.0, 140.0, 36.0), 1), ((240.0, 120.0, 0.0), 1), ((130.0, 200.0, 0.0), 0)]
    for i, (p, free) in enumerate(lines):
        model = list(p)
        model[free] = None
        corrs.append(Correspondence(f"line{i}", tuple(truth.project(p)), tuple(model)))
    sol = solve_camera(corrs, (W, H))
    assert sol.rms_px < 1e-3
    np.testing.assert_allclose(sol.camera.eye, truth.eye, atol=0.05)
    assert sum(not c.is_point for c in sol.correspondences) == len(lines)


def test_outliers_are_flagged_and_do_not_move_the_camera():
    truth = camera(principal_offset=(0.0, -40.0))
    corrs = picks(truth, room_points(), noise_px=0.7, seed=3)
    bad = {2, 9, 17}
    corrs = [Correspondence(c.label, (c.px[0] + 35.0, c.px[1] - 25.0), c.model) if i in bad else c
             for i, c in enumerate(corrs)]
    sol = solve_camera(corrs, (W, H))
    assert abs(sol.camera.f - truth.f) / truth.f < 0.01
    assert {c.label for c, ok in zip(sol.correspondences, sol.inliers) if not ok} == {f"p{i}" for i in bad}


def test_refusals_name_their_reason():
    truth = camera()
    X = room_points()
    with pytest.raises(CameraError, match="at least 6"):
        solve_camera(picks(truth, X[:5]), (W, H))
    lines = [Correspondence(f"l{i}", tuple(truth.project(x)), (x[0], x[1], None)) for i, x in enumerate(X[:6])]
    with pytest.raises(CameraError, match="all three model coordinates"):
        solve_camera(picks(truth, X[:3]) + lines, (W, H))
    with pytest.raises(CameraError, match="duplicate"):
        solve_camera(picks(truth, X) + picks(truth, X[:1]), (W, H))


# ------------------------------------------------------------------ the guard


def test_the_guard_passes_a_well_held_camera():
    truth = camera(pitch=0.0, roll=0.0, principal_offset=(0.0, -40.0))
    sol = solve_camera(picks(truth, room_points(), noise_px=1.0, seed=5), (W, H), attitude="level", aspect="free")
    guard = write_guard(sol)
    assert guard["refused"] == [] and guard["causes"] == []
    assert guard["values"]["eye_along_view_std_in"] < guard["limits"]["eye_std_in"]


def test_the_guard_refuses_coplanar_picks_and_says_why():
    # every pick on the far wall: with the focal length and principal point free, the plane cannot pin them
    truth = camera(principal_offset=(10.0, -30.0))
    X = np.array([(x, 200.0, z) for x in (0.0, 40.0, 100.0, 170.0, 240.0) for z in (0.0, 30.0, 70.0, 96.0)])
    sol = solve_camera(picks(truth, X, noise_px=1.0, seed=2), (W, H))
    guard = write_guard(sol)
    assert sol.geometry["coplanar"]
    assert guard["refused"], guard
    assert any("one plane" in c for c in guard["causes"])


def test_the_guard_refuses_bunched_picks():
    # every pick in one small patch of the frame: the focal length trades against the distance
    truth = camera(pitch=0.0, roll=0.0)
    X = np.array([(x, y, z) for x in (100.0, 112.0, 124.0) for y in (190.0, 200.0) for z in (40.0, 52.0)])
    sol = solve_camera(picks(truth, X, noise_px=1.0, seed=4), (W, H))
    guard = write_guard(sol)
    assert guard["refused"], guard
    assert any("cover only" in c for c in guard["causes"])
