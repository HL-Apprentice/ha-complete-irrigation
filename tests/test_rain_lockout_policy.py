"""Pure rain-lockout policy: blend, day table, ETo scale, zone types, persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.complete_irrigation.rain_lockout import (
    FLAG_RAIN_SOURCE_DISAGREE,
    ZONE_BIRDBATH,
    ZONE_DEEP_DRIP,
    ZONE_FLOWERS_GARDEN,
    ZONE_GERMINATION,
    ZONE_TURF,
    RainBlend,
    StoredRainLockout,
    aggregate_tempest_72h,
    base_lockout_days,
    classify_zone,
    combine_rain_sources,
    decide_lockouts,
    eto_scale_factor,
    extended_until,
    forecast_extends_lockout,
    moisture_releases_early,
    reference_daily_eto,
    release_zone_type,
    scaled_lockout_hours,
    table_days,
    zone_is_garden,
)

NOW = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
MILD_HOURS = 48  # heat ceiling at <= 85F


def _blend(inches: float) -> RainBlend:
    return RainBlend(inches, inches, inches, False, "mean")


def _state(**kwargs) -> StoredRainLockout:
    return StoredRainLockout(**kwargs)


# ── 72h source blend ──────────────────────────────────────────────


def test_sources_that_agree_use_the_mean():
    # Gap 0.05 in is inside the 0.15 in absolute band.
    close = combine_rain_sources(1.00, 1.05)
    assert close.disagree is False
    assert close.used == "mean"
    assert close.inches == pytest.approx(1.025)
    assert close.flag is None

    # Gap 0.30 in, but 40% of the larger (1.30) is 0.52, so they still agree.
    wider = combine_rain_sources(1.00, 1.30)
    assert wider.disagree is False
    assert wider.inches == pytest.approx(1.15)


def test_sources_that_disagree_use_the_lower_plus_a_quarter_of_the_gap():
    blend = combine_rain_sources(0.20, 1.00)
    # tolerance = max(0.15, 0.40 * 1.00) = 0.40; gap 0.80 is outside it.
    assert blend.disagree is True
    assert blend.flag == FLAG_RAIN_SOURCE_DISAGREE
    assert blend.used == "conservative"
    assert blend.inches == pytest.approx(0.20 + 0.25 * 0.80)
    assert blend.tempest_in == pytest.approx(0.20)
    assert blend.open_meteo_in == pytest.approx(1.00)


def test_a_missing_source_falls_back_to_the_other():
    only_meteo = combine_rain_sources(None, 0.50)
    assert only_meteo.used == "open_meteo_only"
    assert only_meteo.inches == pytest.approx(0.50)
    assert only_meteo.disagree is False

    only_tempest = combine_rain_sources(0.40, None)
    assert only_tempest.used == "tempest_only"
    assert only_tempest.inches == pytest.approx(0.40)
    assert only_tempest.disagree is False

    neither = combine_rain_sources(None, float("nan"))
    assert neither.used == "none"
    assert neither.inches is None
    assert neither.disagree is False


# ── day table, ETo scale, heat ceiling ────────────────────────────


@pytest.mark.parametrize(
    ("rain", "expected"),
    [
        (0.09, None),
        (0.10, (1, 1, 1)),
        (0.24, (1, 1, 1)),
        (0.25, (2, 1, 2)),
        (0.49, (2, 1, 2)),
        (0.50, (4, 2, 3)),
        (0.99, (4, 2, 3)),
        (1.00, (6, 3, 4)),
        (2.00, (6, 3, 4)),
        (2.01, (7, 4, 5)),
        (3.50, (7, 4, 5)),
    ],
)
def test_day_table_boundaries(rain, expected):
    assert table_days(rain) == expected


def test_garden_cap_applies_only_above_two_inches():
    assert base_lockout_days(2.5, ZONE_FLOWERS_GARDEN, garden_present=False) == 4
    assert base_lockout_days(2.5, ZONE_FLOWERS_GARDEN, garden_present=True) == 2
    # The cap is not a global flowers+garden ceiling. Under 2 in the table stands.
    assert base_lockout_days(1.5, ZONE_FLOWERS_GARDEN, garden_present=True) == 3
    assert base_lockout_days(2.5, ZONE_DEEP_DRIP, garden_present=True) == 7
    assert base_lockout_days(2.5, ZONE_TURF, garden_present=True) == 5


def test_birdbath_and_germination_have_no_table_days():
    assert base_lockout_days(3.0, ZONE_BIRDBATH) is None
    assert base_lockout_days(3.0, ZONE_GERMINATION) is None


def test_configured_floor_suppresses_lighter_rain():
    assert base_lockout_days(0.40, ZONE_TURF, min_inches=0.50) is None
    assert base_lockout_days(0.50, ZONE_TURF, min_inches=0.50) == 3


def test_eto_scale_clamps_to_half_and_one_and_a_half():
    assert eto_scale_factor(0.12) == pytest.approx(1.0)
    assert eto_scale_factor(0.30) == pytest.approx(0.5)  # 0.40 clamped up
    assert eto_scale_factor(0.06) == pytest.approx(1.5)  # 2.0 clamped down
    assert eto_scale_factor(None) == pytest.approx(0.5)  # fallback 0.25 -> 0.48 -> 0.5
    assert eto_scale_factor(float("nan")) == pytest.approx(0.5)
    # Current and forecast: the hotter demand (larger ETo) wins.
    assert reference_daily_eto(0.10, 0.30) == pytest.approx(0.30)
    assert reference_daily_eto(None, 0) is None


def test_scaled_hours_then_the_heat_ceiling():
    # 2 table-days at a mild 0.12 in/day ETo is 48h, and the mild ceiling allows it.
    assert scaled_lockout_hours(2, 0.12, MILD_HOURS) == pytest.approx(48)
    # Summer ETo clamps the scale to 0.5, so 2 days becomes 24h.
    assert scaled_lockout_hours(2, 0.30, MILD_HOURS) == pytest.approx(24)
    # Cool ETo wants 72h (2d x 1.5 x 24) but the mild ceiling stops it at 48h.
    assert scaled_lockout_hours(2, 0.06, MILD_HOURS) == pytest.approx(48)
    # A 105F day (24h ceiling) cuts a week-long deep-drip figure back to one day.
    assert scaled_lockout_hours(7, 0.12, 24) == pytest.approx(24)


# ── zone types from existing config ───────────────────────────────


def test_zone_type_from_category_name_and_flags():
    assert classify_zone(entity_id="switch.front_lawn") == ZONE_TURF
    assert classify_zone(entity_id="switch.trees", category="trees") == ZONE_DEEP_DRIP
    assert (
        classify_zone(entity_id="switch.beds", category="vegetable_garden") == ZONE_FLOWERS_GARDEN
    )
    assert classify_zone(entity_id="switch.roses", plant_presets=("flower",)) == ZONE_FLOWERS_GARDEN
    assert (
        classify_zone(entity_id="switch.citrus_row", schedule_profiles=("tree",)) == ZONE_DEEP_DRIP
    )
    assert classify_zone(entity_id="switch.birdbath") == ZONE_BIRDBATH
    assert classify_zone(entity_id="switch.zone_9") == ZONE_TURF  # lawn moisture default
    assert classify_zone(entity_id="switch.seed", establishment_active=True) == ZONE_GERMINATION
    # A new-grass name does not exempt the zone once establishment has ended.
    assert classify_zone(entity_id="switch.new_grass") == ZONE_TURF
    assert classify_zone(entity_id="switch.flower_garden") == ZONE_FLOWERS_GARDEN
    assert zone_is_garden(category=None, entity_id="switch.flower_garden") is False
    # A garden name shares the flowers+garden clock. It is not a vegetable
    # garden unless the name or category says vegetable/veg.
    assert classify_zone(entity_id="switch.back_garden") == ZONE_FLOWERS_GARDEN
    assert zone_is_garden(category=None, entity_id="switch.back_garden") is False
    assert classify_zone(entity_id="switch.vegetable_bed") == ZONE_FLOWERS_GARDEN
    assert zone_is_garden(category=None, entity_id="switch.vegetable_bed") is True


# ── decide: per type, release, forecast, birdbath ─────────────────


def test_decide_sets_a_different_clock_per_holding_type():
    update = decide_lockouts(
        None,
        now=NOW,
        rainfall_in=0.60,
        blend=_blend(0.60),
        daily_eto_in=0.12,  # scale 1.0
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=0.0,
        forecast_probability_pct=10,
        min_inches=0.10,
    )
    until = update.state.until_by_type
    # 0.50-1.00 in -> 4 / 2 / 3 days, scale 1, ceiling 48h.
    assert until[ZONE_DEEP_DRIP] == NOW + timedelta(hours=48)  # 4d clamped
    assert until[ZONE_FLOWERS_GARDEN] == NOW + timedelta(hours=48)  # 2d exactly
    assert until[ZONE_TURF] == NOW + timedelta(hours=48)  # 3d clamped
    assert until[ZONE_BIRDBATH] is None
    assert until[ZONE_GERMINATION] is None
    # Same total on the next tick must not push the clocks later.
    again = decide_lockouts(
        update.state,
        now=NOW + timedelta(minutes=1),
        rainfall_in=0.60,
        blend=_blend(0.60),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert again.state.until_by_type[ZONE_DEEP_DRIP] == until[ZONE_DEEP_DRIP]
    assert again.changed is False


def test_hot_ceiling_makes_the_three_clocks_differ():
    update = decide_lockouts(
        None,
        now=NOW,
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=96,  # ceiling high enough that the table shows through
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    until = update.state.until_by_type
    # 0.25-0.50 in -> 2 / 1 / 2 days at scale 1.
    assert until[ZONE_DEEP_DRIP] - NOW == timedelta(days=2)
    assert until[ZONE_FLOWERS_GARDEN] - NOW == timedelta(days=1)
    assert until[ZONE_TURF] - NOW == timedelta(days=2)


def test_birdbath_locks_only_while_rain_is_falling_and_germination_never():
    raining = decide_lockouts(
        None,
        now=NOW,
        rainfall_in=1.0,
        blend=_blend(1.0),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=True,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert raining.state.until_by_type[ZONE_BIRDBATH] == NOW + timedelta(minutes=20)
    assert raining.state.until_by_type[ZONE_GERMINATION] is None

    stopped = decide_lockouts(
        raining.state,
        now=NOW + timedelta(minutes=5),
        rainfall_in=1.0,
        blend=_blend(1.0),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert stopped.state.until_by_type[ZONE_BIRDBATH] is None
    assert stopped.state.until_by_type[ZONE_GERMINATION] is None
    # The holding clocks from the same storm are still there.
    assert stopped.state.until_by_type[ZONE_TURF] == raining.state.until_by_type[ZONE_TURF]


def test_completed_run_releases_one_type_and_the_same_rain_does_not_rearm():
    armed = decide_lockouts(
        None,
        now=NOW,
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=96,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    ).state
    released = release_zone_type(armed, ZONE_TURF)
    assert released.until_by_type[ZONE_TURF] is None
    assert released.until_by_type[ZONE_DEEP_DRIP] == armed.until_by_type[ZONE_DEEP_DRIP]
    again = decide_lockouts(
        released,
        now=NOW + timedelta(hours=1),
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=96,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert again.state.until_by_type[ZONE_TURF] is None


def test_moisture_can_release_early_and_cannot_extend():
    assert moisture_releases_early(10, 21) is True
    assert moisture_releases_early(21, 21) is False
    assert moisture_releases_early(80, 21) is False
    assert moisture_releases_early(float("nan"), 21) is False

    armed = decide_lockouts(
        None,
        now=NOW,
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=96,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    ).state
    deep_until = armed.until_by_type[ZONE_DEEP_DRIP]
    turf_until = armed.until_by_type[ZONE_TURF]

    # A wet reading is simply not in the dry set. Nothing moves later.
    wet = decide_lockouts(
        armed,
        now=NOW + timedelta(hours=1),
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=96,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert wet.state.until_by_type[ZONE_TURF] == turf_until
    assert wet.state.until_by_type[ZONE_DEEP_DRIP] == deep_until
    assert wet.state.released_zones == {}

    dry = decide_lockouts(
        armed,
        now=NOW + timedelta(hours=1),
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=96,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
        dry_zones={"switch.front_lawn": ZONE_TURF},
    )
    # The type clock stays. Only the dry zone is exempt.
    assert dry.state.until_by_type[ZONE_TURF] == turf_until
    assert dry.state.until_by_type[ZONE_DEEP_DRIP] == deep_until
    assert dry.state.released_zones == {"switch.front_lawn": ZONE_TURF}

    same_tick = decide_lockouts(
        None,
        now=NOW,
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=96,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
        dry_zones={"switch.front_lawn": ZONE_TURF},
    )
    assert "switch.front_lawn" not in same_tick.state.released_zones


def test_forecast_extends_an_active_lockout_once_and_respects_the_ceiling():
    assert forecast_extends_lockout(0.25, 60) is True
    assert forecast_extends_lockout(0.24, 90) is False
    assert forecast_extends_lockout(0.50, 59) is False
    assert forecast_extends_lockout(0.50, None) is False

    start = StoredRainLockout(
        until_by_type={
            ZONE_TURF: NOW + timedelta(hours=12),
            ZONE_DEEP_DRIP: NOW + timedelta(hours=48),
        },
        last_event_rain_in=0.30,
    )
    extended = decide_lockouts(
        start,
        now=NOW,
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=48,
        garden_present=False,
        active_rain=False,
        forecast_in=0.40,
        forecast_probability_pct=70,
    )
    # 12h + 1 day = 36h, still under the 48h ceiling.
    assert extended.state.until_by_type[ZONE_TURF] == NOW + timedelta(hours=36)
    # Already at the ceiling: the extra day is not allowed to grow past it.
    assert extended.state.until_by_type[ZONE_DEEP_DRIP] == NOW + timedelta(hours=48)
    assert ZONE_TURF in extended.state.forecast_extended
    assert ZONE_DEEP_DRIP in extended.state.forecast_extended

    again = decide_lockouts(
        extended.state,
        now=NOW + timedelta(minutes=30),
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=48,
        garden_present=False,
        active_rain=False,
        forecast_in=0.40,
        forecast_probability_pct=90,
    )
    assert again.state.until_by_type[ZONE_TURF] == NOW + timedelta(hours=36)

    # No active clock -> the forecast does not start one.
    idle = extended_until(None, NOW, max_hours=48)
    assert idle is None


def test_persistence_round_trip():
    original = StoredRainLockout(
        until_by_type={
            ZONE_DEEP_DRIP: NOW + timedelta(days=3),
            ZONE_FLOWERS_GARDEN: NOW + timedelta(days=1),
            ZONE_TURF: NOW + timedelta(hours=36),
            ZONE_BIRDBATH: NOW + timedelta(minutes=15),
        },
        last_event_rain_in=0.82,
        rain_source_disagree=True,
        forecast_extended={ZONE_TURF},
        sources={"used": "conservative", "combined_in": 0.82},
        previous_today_in=0.20,
    )
    restored = StoredRainLockout.from_dict(original.to_dict())
    assert restored.until_by_type[ZONE_DEEP_DRIP] == original.until_by_type[ZONE_DEEP_DRIP]
    assert (
        restored.until_by_type[ZONE_FLOWERS_GARDEN] == original.until_by_type[ZONE_FLOWERS_GARDEN]
    )
    assert restored.until_by_type[ZONE_TURF] == original.until_by_type[ZONE_TURF]
    assert restored.until_by_type[ZONE_BIRDBATH] == original.until_by_type[ZONE_BIRDBATH]
    assert restored.until_by_type[ZONE_GERMINATION] is None
    assert restored.last_event_rain_in == pytest.approx(0.82)
    assert restored.rain_source_disagree is True
    assert restored.forecast_extended == {ZONE_TURF}
    assert restored.sources["used"] == "conservative"
    assert restored.previous_today_in == pytest.approx(0.20)
    # Garbage on disk must not raise or invent a lockout.
    empty = StoredRainLockout.from_dict({"lockout_until": {"turf": "not-a-date"}, "nope": 1})
    assert empty.until_by_type[ZONE_TURF] is None
    assert empty.last_event_rain_in is None
    assert empty.baseline_seeded is False


def test_tempest_statistics_accept_float_timestamps_and_a_72h_window():
    from zoneinfo import ZoneInfo

    now = NOW
    # HA 2023.3+ (verified on 2025.1) returns start as a POSIX timestamp.
    # The bucket that starts exactly 72h ago is the 73rd hour and is excluded.
    rows = [
        {"start": (now - timedelta(hours=72)).timestamp(), "sum": 9.0, "state": 0.0},
        {"start": (now - timedelta(hours=70)).timestamp(), "sum": 10.0, "state": 0.0},
        {"start": (now - timedelta(hours=2)).timestamp(), "sum": 10.5, "state": 0.10},
    ]
    # Growth is 0.5. Live 0.25 is today's reading, ahead of the last row's
    # state (0.10), not ahead of the cumulative sum, so 0.15 is added.
    assert aggregate_tempest_72h(
        rows, now=now, state_class="total_increasing", live_today_in=0.25, unit="in"
    ) == pytest.approx(0.65)
    # Live below that same-day state adds nothing, even though it is far
    # below the cumulative sum.
    assert aggregate_tempest_72h(
        rows, now=now, state_class="total_increasing", live_today_in=0.05, unit="in"
    ) == pytest.approx(0.5)
    # ISO and datetime inputs still parse.
    iso_rows = [
        {"start": (now - timedelta(hours=70)), "sum": 10.0, "state": 0.0},
        {"start": (now - timedelta(hours=2)).isoformat(), "sum": 10.5, "state": 0.10},
    ]
    assert aggregate_tempest_72h(
        iso_rows, now=now, state_class="total_increasing", live_today_in=0.25, unit="in"
    ) == pytest.approx(0.65)

    window_start = now - timedelta(hours=72)
    partial = [
        {"start": window_start.timestamp(), "max": 5.0},
        {"start": (window_start + timedelta(hours=2)).timestamp(), "max": 0.4},
        {"start": (window_start + timedelta(hours=6)).timestamp(), "max": 0.9},
        {"start": (now - timedelta(hours=3)).timestamp(), "max": 0.2},
        {"start": (now - timedelta(hours=1)).timestamp(), "max": 0.35},
    ]
    # The exact -72h row (peak 5.0) is outside the window. Partial first day
    # contributes 0.9 - 0.4 = 0.5. Today is max(0.35, live 0.5) = 0.5.
    assert aggregate_tempest_72h(
        partial, now=now, state_class=None, live_today_in=0.5, unit="in"
    ) == pytest.approx(1.0)

    # Day buckets follow local time, not the UTC date of the timestamp.
    local = datetime(2026, 7, 15, 2, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    yesterday_utc = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)  # 23:00 the previous local day
    today_utc = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)  # 01:00 local
    local_rows = [
        {"start": yesterday_utc.timestamp(), "max": 0.8},
        {"start": today_utc.timestamp(), "max": 0.1},
    ]
    assert aggregate_tempest_72h(
        local_rows, now=local, state_class=None, live_today_in=0.4, unit="in"
    ) == pytest.approx(1.2)


def test_source_dropout_and_recovery_does_not_arm():
    blend = RainBlend(0.45, 0.30, 0.60, True, "conservative")
    seeded = decide_lockouts(
        StoredRainLockout.from_dict({}),
        now=NOW,
        rainfall_in=0.45,
        blend=blend,
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
        seed_only=True,
    )
    assert seeded.state.baseline_seeded is True
    assert seeded.state.baseline_by_source["tempest"] == pytest.approx(0.30)
    assert seeded.state.baseline_by_source["open_meteo"] == pytest.approx(0.60)
    assert all(until is None for until in seeded.state.until_by_type.values())

    # Tempest drops out. Open-Meteo alone is 0.60, which is not a rise of a
    # source that was in both samples.
    dropped = decide_lockouts(
        seeded.state,
        now=NOW + timedelta(minutes=1),
        rainfall_in=0.60,
        blend=RainBlend(0.60, None, 0.60, False, "open_meteo_only"),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert all(until is None for until in dropped.state.until_by_type.values())
    assert dropped.state.baseline_by_source["tempest"] == pytest.approx(0.30)
    assert dropped.armed_types == frozenset()

    recovered = decide_lockouts(
        dropped.state,
        now=NOW + timedelta(minutes=2),
        rainfall_in=0.45,
        blend=blend,
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert all(until is None for until in recovered.state.until_by_type.values())

    # An older store has a blended baseline and no per-source memory.
    # Switching to Open-Meteo-only 0.60 must not arm.
    legacy_doc = StoredRainLockout(last_event_rain_in=0.30, baseline_seeded=True)
    swapped = decide_lockouts(
        legacy_doc,
        now=NOW,
        rainfall_in=0.60,
        blend=RainBlend(0.60, None, 0.60, False, "open_meteo_only"),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert all(until is None for until in swapped.state.until_by_type.values())
    assert swapped.state.baseline_by_source["open_meteo"] == pytest.approx(0.60)


def test_tiny_source_revision_does_not_arm():
    seeded = decide_lockouts(
        StoredRainLockout.from_dict({}),
        now=NOW,
        rainfall_in=0.50,
        blend=RainBlend(0.50, 0.40, 0.60, False, "mean"),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
        seed_only=True,
    )
    # Open-Meteo ticks up 0.005 in. Under the 0.01 in rise floor.
    revised = decide_lockouts(
        seeded.state,
        now=NOW + timedelta(minutes=30),
        rainfall_in=0.5025,
        blend=RainBlend(0.5025, 0.40, 0.605, False, "mean"),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert revised.armed_types == frozenset()
    assert all(until is None for until in revised.state.until_by_type.values())
    # 0.02 in on the same source is a real addition and arms.
    real = decide_lockouts(
        revised.state,
        now=NOW + timedelta(minutes=60),
        rainfall_in=0.51,
        blend=RainBlend(0.51, 0.40, 0.62, False, "mean"),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert ZONE_TURF in real.armed_types


def test_rise_that_does_not_move_the_end_clears_released_zones():
    armed = decide_lockouts(
        None,
        now=NOW,
        rainfall_in=0.30,
        blend=_blend(0.30),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    # 0.30 in at a 48h ceiling: turf is already at the ceiling (2 days).
    held = armed.state.with_updates(released_zones={"switch.front_lawn": ZONE_TURF})
    turf_until = held.until_by_type[ZONE_TURF]
    assert turf_until == NOW + timedelta(hours=48)
    again = decide_lockouts(
        held,
        now=NOW,
        rainfall_in=0.60,
        blend=_blend(0.60),
        daily_eto_in=0.12,
        max_hours=MILD_HOURS,
        garden_present=False,
        active_rain=False,
        forecast_in=None,
        forecast_probability_pct=None,
    )
    assert again.state.until_by_type[ZONE_TURF] == turf_until
    assert ZONE_TURF in again.armed_types
    assert "switch.front_lawn" not in again.state.released_zones
