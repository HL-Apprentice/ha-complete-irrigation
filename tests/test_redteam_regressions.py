"""Red-team regressions (v1.62.2).

Found by an adversarial review whose only goal was to make the user silently
lose or corrupt data. Both bugs below were introduced by recent work and both
were reproduced numerically before being fixed, so these tests exist to keep
them dead.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.complete_irrigation.plant import PlantRecord, reproject_plants
from custom_components.complete_irrigation.yard_map import bbox_from_center
from custom_components.complete_irrigation.zone_region import (
    _MAX_REGIONS,
    ZoneRegion,
    ZoneRegionStore,
)

LAT, LON = 33.30, -111.80
FRAME = bbox_from_center(LAT, LON, 60.0)
_M_LAT = 111_320.0


def _plant(**kw) -> PlantRecord:
    base = {
        "id": "aaaaaaaaaaaa",
        "name": "Oleander",
        "wucols_category": "moderate",
        "canopy_area_sqft": 40.0,
        "zone_entity_id": "switch.zone_a",
    }
    base.update(kw)
    return PlantRecord(**base)


def _region(idx: int = 0) -> ZoneRegion:
    import math

    half_lat = 5.0 / _M_LAT
    half_lon = 5.0 / (_M_LAT * math.cos(math.radians(LAT)))
    return ZoneRegion(
        id=f"r{idx:04d}",
        zone_entity_id="switch.back_yard_grass_blue",
        north=LAT + half_lat,
        south=LAT - half_lat,
        east=LON + half_lon,
        west=LON - half_lon,
    )


# ── duplicating a plant must not inherit the source's ground anchor ──


def test_duplicate_must_clear_the_anchor_not_just_the_map_position():
    """A duplicate presents as UNPLACED (map_x/map_y cleared) but used to keep the
    source's anchor_lat/lon. Since reproject_plants trusts the anchor, the very
    next zoom/nudge/refresh re-derived a position and the copy materialised
    stacked exactly on top of the original — the dual-truth state the whole
    anchor design exists to prevent."""
    src = _plant(map_x=0.25, map_y=0.25, anchor_lat=LAT, anchor_lon=LON)
    # Exactly what handle_duplicate_plant builds (post-fix it must also null the anchor).
    copy = replace(
        src,
        id="bbbbbbbbbbbb",
        photos=(),
        map_x=None,
        map_y=None,
        anchor_lat=None,
        anchor_lon=None,
    )
    assert copy.map_x is None and copy.anchor_lat is None, "unplaced means BOTH are clear"

    # One frame change of any kind must leave the copy unplaced.
    updated, moved, dropped = reproject_plants([copy], FRAME, bbox_from_center(LAT, LON, 61.0))
    assert updated == [] and moved == 0 and dropped == 0


def test_an_inherited_anchor_would_resurrect_the_copy():
    """Pins WHY the fix matters: with the anchor left on, the copy comes back."""
    src = _plant(map_x=0.25, map_y=0.25, anchor_lat=LAT, anchor_lon=LON)
    buggy_copy = replace(src, id="bbbbbbbbbbbb", photos=(), map_x=None, map_y=None)
    updated, moved, _ = reproject_plants([buggy_copy], FRAME, bbox_from_center(LAT, LON, 61.0))
    assert moved == 1  # it comes back...
    assert updated[0].map_x is not None  # ...at the source's position


# ── the region cap must be enforced where regions are CREATED ────────


def test_region_cap_is_enforced_on_add_not_only_on_load():
    """The 200-region cap was enforced only in from_serializable. Anything past
    200 persisted fine, was truncated on the next restart, and was then erased
    from disk by the next save — permanent loss, no undo, one log line."""
    store = ZoneRegionStore()
    for i in range(_MAX_REGIONS):
        store.add(_region(i))
    assert len(store.all()) == _MAX_REGIONS
    with pytest.raises(ValueError, match="at most"):
        store.add(_region(_MAX_REGIONS))  # the 201st must be REFUSED, not silently kept
    assert len(store.all()) == _MAX_REGIONS


def test_upsert_of_an_existing_region_is_not_blocked_by_the_cap():
    """Editing region #7 when 200 exist must still work — the cap bounds NEW
    records, it must not brick an at-capacity store."""
    store = ZoneRegionStore()
    for i in range(_MAX_REGIONS):
        store.add(_region(i))
    existing = _region(7)
    store.upsert(existing)  # must not raise
    assert len(store.all()) == _MAX_REGIONS


def test_the_survivors_of_a_load_truncation_are_not_re_saved_as_the_whole_set():
    """Belt and braces: a file that somehow holds >cap records still loads capped
    (corrupt-file backstop), which is why the write-side guard is the real fix."""
    many = [_region(i).to_dict() for i in range(_MAX_REGIONS + 5)]
    loaded = ZoneRegionStore.from_serializable(many)
    assert len(loaded.all()) == _MAX_REGIONS
