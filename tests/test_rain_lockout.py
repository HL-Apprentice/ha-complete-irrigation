"""Rain-lockout policy (pure). No Home Assistant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.complete_irrigation.rain_lockout import (
    DURABLE_ZONE_TYPES,
    FLOWERS_GARDEN_OVER_2_UNCAPPED_DAYS,
    RAIN_SOURCE_DISAGREE,
    TYPE_BIRDBATH,
    TYPE_DEEP_DRIP,
    TYPE_FLOWERS_GARDEN,
    TYPE_GERMINATION,
    TYPE_TURF,
    LockoutState,
    active_rain,
    base_lockout_days,
    classify_zone_type,
    combine_rain_totals,
    eto_scale_factor,
    forecast_should_extend,
    moisture_releases_lockout,
    parse_open_meteo_rain,
    scaled_lockout_hours,
    select_lockout_daily_eto,
    sources_agree,
    sum_daily_peaks,
)

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def test_sources_agree_inside_absolute_or_relative_band():
    # 0.10 in gap is inside the 0.15 in floor.
    assert sources_agree(0.40, 0.50)
    # Large totals: 0.30 in gap is inside 40% of 1.30 (0.52).
    assert sources_agree(1.00, 1.30)
    # 1.00 in gap is outside both 0.15 and 40% of 1.20 (0.48).
    assert not sources_agree(0.20, 1.20)


def test_combine_uses_the_mean_when_sources_agree():
    combined = combine_rain_totals(0.40, 0.50)
    assert combined.disagree is False
    assert combined.source == "mean"
    assert combined.inches == pytest.approx(0.45)
    assert RAIN_SOURCE_DISAGREE  # the flag name the coordinator publishes


def test_combine_disagreement_uses_lower_plus_quarter_gap():
    combined = combine_rain_totals(1.20, 0.20)
    assert combined.disagree is True
    assert combined.source == "conservative"
    # lower 0.20 + 0.25 * 1.00 = 0.45
    assert combined.inches == pytest.approx(0.45)
    assert combined.tempest_in == pytest.approx(1.20)
    assert combined.meteo_in == pytest.approx(0.20)


def test_combine_falls_back_when_one_source_is_missing():
    only_meteo = combine_rain_totals(None, 0.40)
    assert only_meteo.inches == pytest.approx(0.40)
    assert only_meteo.source == "open_meteo"
    assert only_meteo.disagree is False

    only_tempest = combine_rain_totals(0.30, None)
    assert only_tempest.source == "tempest"
    assert only_tempest.inches == pytest.approx(0.30)
    assert only_tempest.disagree is False

    neither = combine_rain_totals(None, float("nan"))
    assert neither.inches is None
    assert neither.source == "none"
    assert neither.disagree is False


def test_combine_anchor_is_the_later_rain():
    earlier = NOW - timedelta(days=1)
    combined = combine_rain_totals(0.4, 0.42, tempest_last_rain_at=earlier, meteo_last_rain_at=NOW)
    assert combined.last_rain_at == NOW
    one_sided = combine_rain_totals(None, 0.4, meteo_last_rain_at=earlier)
    assert one_sided.last_rain_at == earlier


@pytest.mark.parametrize(
    "rain,expected",
    [
        (0.09, None),
        (0.10, (1, 1, 1)),
        (0.249, (1, 1, 1)),
        (0.25, (2, 1, 2)),
        (0.49, (2, 1, 2)),
        (0.50, (4, 2, 3)),
        (0.99, (4, 2, 3)),
        (1.00, (6, 3, 4)),
        (2.00, (6, 3, 4)),  # "1-2" includes 2.00
        (2.01, (7, 2, 5)),  # ">2", garden capped at 2 (uncapped cell was 4)
        (5.00, (7, 2, 5)),
    ],
)
def test_day_table(rain, expected):
    got = tuple(base_lockout_days(rain, zone) for zone in DURABLE_ZONE_TYPES)
    assert got == ((None, None, None) if expected is None else expected)
    if rain > 2:
        assert FLOWERS_GARDEN_OVER_2_UNCAPPED_DAYS == 4
        assert base_lockout_days(rain, TYPE_FLOWERS_GARDEN) == 2


def test_birdbath_and_germination_have_no_day_hold():
    assert base_lockout_days(1.5, TYPE_BIRDBATH) is None
    assert base_lockout_days(1.5, TYPE_GERMINATION) is None


def test_eto_scale_and_clamps():
    assert eto_scale_factor(0.12) == pytest.approx(1.0)
    assert eto_scale_factor(0.24) == pytest.approx(0.5)  # 0.5 exactly, clamp floor
    assert eto_scale_factor(0.48) == pytest.approx(0.5)  # 0.25 → clamp 0.5
    assert eto_scale_factor(0.06) == pytest.approx(1.5)  # 2.0 → clamp 1.5
    assert eto_scale_factor(0.08) == pytest.approx(1.5)
    assert eto_scale_factor(None) == 1.0
    assert eto_scale_factor(0.0) == 1.0
    assert eto_scale_factor(float("nan")) == 1.0


def test_daily_eto_uses_the_hotter_of_current_and_forecast():
    assert select_lockout_daily_eto(0.20, 0.35) == pytest.approx(0.35)
    assert select_lockout_daily_eto(0.20, None) == pytest.approx(0.20)
    assert select_lockout_daily_eto(None, None) is None
    assert select_lockout_daily_eto(-1, float("nan"), 0.1) == pytest.approx(0.1)


def test_scaled_hours_then_heat_ceiling():
    # Mild day, reference ETo: 7 deep-drip days = 168h, ceiling at 80F is 48h.
    assert scaled_lockout_hours(3.0, TYPE_DEEP_DRIP, 0.12, 80) == 48
    # Hot forecast ETo shortens before the ceiling: 7 * 0.5 * 24 = 84, then 18h at 115F.
    assert scaled_lockout_hours(3.0, TYPE_DEEP_DRIP, 0.30, 115) == 18
    # Missing temperature keeps the existing 24h fail-safe ceiling.
    assert scaled_lockout_hours(3.0, TYPE_DEEP_DRIP, 0.12, None) == 24
    # Under the ceiling the scale is visible: 1 garden day * 0.5 = 12h at 105F (ceiling 24).
    assert scaled_lockout_hours(0.20, TYPE_FLOWERS_GARDEN, 0.30, 105) == 12
    # A caller-supplied ceiling overrides the temperature curve.
    assert scaled_lockout_hours(0.20, TYPE_TURF, 0.12, 80, max_hours=5) == 5


def test_forecast_extension_rule():
    assert forecast_should_extend(0.25, 60)
    assert forecast_should_extend(0.40, 0.60)  # probability on a 0-1 scale
    assert not forecast_should_extend(0.24, 90)
    assert not forecast_should_extend(0.50, 59)
    assert not forecast_should_extend(1.0, None)  # amount alone never extends


def test_moisture_releases_only_when_wet_enough():
    assert moisture_releases_lockout(40, 31) is True
    assert moisture_releases_lockout(31, 31) is True
    assert moisture_releases_lockout(20, 31) is False  # dry must not extend anything
    assert moisture_releases_lockout(None, 31) is False
    assert moisture_releases_lockout(float("nan"), 31) is False


def test_active_rain_is_not_a_daily_total():
    assert active_rain(["rainy"])
    assert active_rain(["sunny"], precip_rate_in_per_h=0.05)
    assert active_rain([], current_hour_precip_mm=0.2)
    assert not active_rain(["sunny"])
    assert not active_rain(["cloudy"], precip_rate_in_per_h=0.0, current_hour_precip_mm=0.0)
    assert not active_rain(None)


def test_zone_type_from_existing_signals():
    assert (
        classify_zone_type(created_via="establishment", category="lawn", independent=True)
        == TYPE_GERMINATION
    )
    assert classify_zone_type(schedule_name="New planting front cycle 1") == TYPE_GERMINATION
    assert classify_zone_type(independent=True, category="lawn") == TYPE_BIRDBATH
    assert classify_zone_type(friendly_name="Front birdbath") == TYPE_BIRDBATH
    assert classify_zone_type(category="lawn") == TYPE_TURF
    assert classify_zone_type(category="vegetable_garden") == TYPE_FLOWERS_GARDEN
    assert classify_zone_type(category="citrus") == TYPE_DEEP_DRIP
    assert classify_zone_type(category="trees") == TYPE_DEEP_DRIP
    assert classify_zone_type(split_profile="grass") == TYPE_TURF
    assert classify_zone_type(split_profile="flower") == TYPE_FLOWERS_GARDEN
    assert classify_zone_type(split_profile="tree") == TYPE_DEEP_DRIP
    assert classify_zone_type(plant_presets=["shrub", "shrub", "flower"]) == TYPE_DEEP_DRIP
    assert classify_zone_type(friendly_name="Back garden") == TYPE_FLOWERS_GARDEN
    assert classify_zone_type(zone_entity_id="switch.front_lawn") == TYPE_TURF
    # No signal → middle hold, not "no lockout".
    assert classify_zone_type(zone_entity_id="switch.zone_3") == TYPE_TURF


def test_tempest_daily_peaks_sum_the_resetting_accumulator():
    samples = [
        (NOW - timedelta(hours=50), 0.10),
        (NOW - timedelta(hours=48), 0.40),  # yesterday's peak
        (NOW - timedelta(hours=47), 0.05),  # reset, ignored as the day's peak
        (NOW - timedelta(hours=2), 0.20),
        (NOW, 0.25),  # today's peak
    ]
    total, last = sum_daily_peaks(samples, NOW)
    assert total == pytest.approx(0.65)  # 0.40 + 0.25
    assert last == NOW
    assert sum_daily_peaks([], NOW) == (None, None)


def test_open_meteo_72h_and_forecast_extension_parse():
    payload = {
        "daily": {
            "time": ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"],
            "precipitation_sum": [0.0, 12.7, 0.0, 2.54, 0.0],  # 0.5 in + 0.1 in inside 72h
            "et0_fao_evapotranspiration": [4.0, 5.0, 6.0, 7.62, 2.54],
            "temperature_2m_max": [20.0, 21.0, 22.0, 40.0, 30.0],
        },
        "hourly": {
            "time": ["2026-11-04T14:00", "2026-11-04T15:00", "2026-11-05T14:00"],
            "precipitation": [0.0, 6.35, 0.0],  # 0.25 in in the next hours
            "precipitation_probability": [10, 70, 5],
        },
    }
    parsed = parse_open_meteo_rain(payload, NOW)
    # 72h back from 15:00 on the 4th touches the 1st (15:00) through today.
    # 12.7mm + 2.54mm = 0.6 in. The 1st is inside the date window (start date).
    assert parsed["precip_72h_in"] == pytest.approx((12.7 + 2.54) / 25.4)
    assert parsed["forecast_extends"] is True
    assert parsed["forecast_24h_in"] == pytest.approx(6.35 / 25.4)
    assert parsed["forecast_prob_max"] == 70
    # Hotter of today (7.62mm) and tomorrow (2.54mm).
    assert parsed["forecast_daily_eto_in"] == pytest.approx(7.62 / 25.4)
    assert parsed["forecast_high_f"] == pytest.approx(104.0)
    assert parsed["current_hour_precip_mm"] == pytest.approx(6.35)
    assert parsed["last_rain_at"] is not None


def test_open_meteo_garbage_is_empty():
    parsed = parse_open_meteo_rain("nope", NOW)
    assert parsed["precip_72h_in"] is None
    assert parsed["forecast_extends"] is False


def test_lockout_state_round_trip():
    original = LockoutState(
        lockout_until={
            TYPE_DEEP_DRIP: NOW + timedelta(days=3),
            TYPE_FLOWERS_GARDEN: NOW + timedelta(days=1),
            TYPE_TURF: NOW + timedelta(days=2),
        },
        event_rain_in=0.62,
        event_anchor=NOW - timedelta(hours=5),
        released_zones={"switch.front_lawn"},
        forecast_extended_on={TYPE_TURF: "2026-11-04"},
        rain_source_disagree=True,
        last_tempest_in=0.80,
        last_open_meteo_in=0.40,
        last_combined_in=0.50,
        rain_source="conservative",
    )
    restored = LockoutState.from_dict(original.to_dict())
    assert restored.lockout_until == original.lockout_until
    assert restored.event_rain_in == pytest.approx(0.62)
    assert restored.event_anchor == original.event_anchor
    assert restored.released_zones == {"switch.front_lawn"}
    assert restored.forecast_extended_on == {TYPE_TURF: "2026-11-04"}
    assert restored.rain_source_disagree is True
    assert restored.last_combined_in == pytest.approx(0.50)
    assert restored.rain_source == "conservative"


def test_lockout_state_load_is_tolerant():
    restored = LockoutState.from_dict(
        {
            "lockout_until": {
                "turf": "not-a-date",
                "nope": NOW.isoformat(),
                "deep_drip": NOW.isoformat(),
            },
            "released_zones": "switch.lawn",
            "event_rain_in": "lots",
        }
    )
    assert set(restored.lockout_until) == {TYPE_DEEP_DRIP}
    assert restored.released_zones == set()
    assert restored.event_rain_in is None
    assert LockoutState.from_dict(None).lockout_until == {}
    assert LockoutState.from_dict([]).rain_source == "none"
