"""Aerial display rotation (v1.59) — normalize_rotation + cover_scale.

Rotation is a pure presentation transform over a north-up export: the bbox and
every stored marker position are untouched. These cover the two bits of maths
the panel mirrors in JS, so a mismatch here is a mis-placed marker there.
"""

from __future__ import annotations

import math

from custom_components.complete_irrigation.yard_map import cover_scale, normalize_rotation

# ── normalize_rotation ──────────────────────────────────────────────


def test_plain_angles_unchanged():
    assert normalize_rotation(0) == 0.0
    assert normalize_rotation(12) == 12.0
    assert normalize_rotation(-45.5) == -45.5
    assert normalize_rotation(180) == 180.0


def test_wraps_rather_than_clamping():
    # Repeated ↻ taps must never jam at a limit: 181° is the same view as -179°.
    assert normalize_rotation(181) == -179.0
    assert normalize_rotation(360) == 0.0
    assert normalize_rotation(365) == 5.0
    assert normalize_rotation(-360) == 0.0
    assert normalize_rotation(-181) == 179.0
    assert normalize_rotation(720 + 30) == 30.0


def test_junk_and_non_finite_are_zero():
    for bad in (None, "", "abc", [], {}, float("nan"), float("inf"), float("-inf")):
        assert normalize_rotation(bad) == 0.0


def test_numeric_strings_accepted():
    assert normalize_rotation("15") == 15.0
    assert normalize_rotation("-7.5") == -7.5


def test_no_negative_zero():
    # -0.0 would serialize as "-0" and read as a bug in the UI.
    assert math.copysign(1.0, normalize_rotation(-0.0)) == 1.0
    assert math.copysign(1.0, normalize_rotation(360)) == 1.0


# ── cover_scale ─────────────────────────────────────────────────────


def test_no_rotation_needs_no_upscale():
    assert cover_scale(1536, 1285, 0) == 1.0
    assert cover_scale(100, 100, 0) == 1.0


def test_square_45_degrees_is_sqrt2():
    assert math.isclose(cover_scale(500, 500, 45), math.sqrt(2))  # |cos|+|sin| = √2


def test_scale_grows_with_angle_then_symmetric():
    a = cover_scale(1536, 1285, 5)
    b = cover_scale(1536, 1285, 15)
    assert 1.0 < a < b
    # Direction of turn cannot matter.
    assert cover_scale(1536, 1285, -15) == b
    # 90° is the worst case for a non-square frame (long side across the short).
    assert cover_scale(1536, 1285, 90) == max(1536 / 1285, 1285 / 1536)


def test_covers_the_frame_for_real_aspect_ratios():
    # The property that matters: after rotating by deg and scaling by k, the
    # image must still cover its own w x h frame — i.e. every frame corner is
    # inside the scaled, rotated image. Verified numerically, not by formula.
    for w, h in ((1536, 1285), (1024, 1024), (800, 1400)):
        for deg in range(-180, 181, 7):
            k = cover_scale(w, h, deg)
            r = math.radians(deg)
            cos, sin = math.cos(-r), math.sin(-r)
            for cx, cy in ((-w / 2, -h / 2), (w / 2, -h / 2), (-w / 2, h / 2), (w / 2, h / 2)):
                # frame corner, expressed in the (unrotated) image's own frame
                ix = cx * cos - cy * sin
                iy = cx * sin + cy * cos
                assert abs(ix) <= k * w / 2 + 1e-6, (w, h, deg)
                assert abs(iy) <= k * h / 2 + 1e-6, (w, h, deg)


def test_degenerate_sizes_fall_back_to_one():
    for w, h in ((0, 100), (100, 0), (-5, 10), (None, 10), ("x", 10)):
        assert cover_scale(w, h, 30) == 1.0
