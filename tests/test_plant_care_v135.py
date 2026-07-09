"""v1.35 plant-care bundle — light surveys, care tasks, watering diagnosis,
and the PlantRecord field extensions. Pure-logic tests, no HA."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.complete_irrigation.care_tasks import (
    CareTask,
    CareTaskStore,
)
from custom_components.complete_irrigation.light_survey import (
    MAX_LUX,
    MAX_SURVEYS_KEPT,
    VERDICT_NO_RANGE,
    VERDICT_OPTIMAL,
    VERDICT_TOO_HIGH,
    VERDICT_TOO_LOW,
    clean_stored_surveys,
    light_verdict,
    make_survey_entry,
    summarize_samples,
    validate_lux_range,
    verdict_text,
)
from custom_components.complete_irrigation.plant import PlantRecord
from custom_components.complete_irrigation.run_history import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_SKIPPED,
    RunHistoryRecord,
)
from custom_components.complete_irrigation.watering_diagnosis import (
    STATUS_OK,
    STATUS_POSSIBLE_OVER,
    STATUS_POSSIBLE_UNDER,
    STATUS_UNKNOWN,
    diagnose_zone,
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


# ════════════════════════════════════════════════════════════════════
# light_survey
# ════════════════════════════════════════════════════════════════════


def test_validate_lux_range_ok():
    assert validate_lux_range(430, 1184) == (430, 1184)
    assert validate_lux_range("500", 2000.7) == (500, 2000)


@pytest.mark.parametrize(
    "low,high",
    [
        (1000, 500),  # inverted
        (0, 500),  # low must be > 0
        (-5, 500),
        (100, MAX_LUX + 1),
        (float("nan"), 500),
        (100, float("inf")),
        ("abc", 500),
        (None, 500),
        (500, 500),  # low must be < high
    ],
)
def test_validate_lux_range_rejects(low, high):
    with pytest.raises(ValueError):
        validate_lux_range(low, high)


def test_summarize_samples_filters_junk():
    s = summarize_samples([100, "250", None, "unavailable", float("nan"), -3, MAX_LUX * 2, 400])
    assert s is not None
    assert s["samples"] == 3  # 100, 250, 400
    assert s["lux_min"] == 100
    assert s["lux_max"] == 400
    assert s["lux_avg"] == 250


def test_summarize_samples_empty_and_all_junk():
    assert summarize_samples([]) is None
    assert summarize_samples(["unknown", None, float("inf")]) is None


def test_light_verdict_directions():
    assert light_verdict(100, 430, 1184) == VERDICT_TOO_LOW
    assert light_verdict(800, 430, 1184) == VERDICT_OPTIMAL
    assert light_verdict(430, 430, 1184) == VERDICT_OPTIMAL  # inclusive bounds
    assert light_verdict(1184, 430, 1184) == VERDICT_OPTIMAL
    assert light_verdict(5000, 430, 1184) == VERDICT_TOO_HIGH
    assert light_verdict(800, None, None) == VERDICT_NO_RANGE


def test_verdict_text_mentions_plant():
    for v in (VERDICT_TOO_LOW, VERDICT_OPTIMAL, VERDICT_TOO_HIGH, VERDICT_NO_RANGE):
        assert "Citrus" in verdict_text(v, "Citrus")


def test_make_survey_entry_rejects_bad_verdict():
    summary = {"lux_min": 1.0, "lux_avg": 2.0, "lux_max": 3.0, "samples": 3}
    with pytest.raises(ValueError):
        make_survey_entry(
            ts=1, minutes=10, sensor_entity_id="sensor.x", summary=summary, verdict="nope"
        )


def test_clean_stored_surveys_drops_junk_sorts_and_caps():
    good = {
        "ts": 100,
        "minutes": 10,
        "sensor": "sensor.lux",
        "lux_min": 1,
        "lux_avg": 2,
        "lux_max": 3,
        "samples": 5,
        "verdict": VERDICT_OPTIMAL,
    }
    entries = [dict(good, ts=i) for i in range(MAX_SURVEYS_KEPT + 10)]
    entries.append({"ts": "bad"})
    entries.append(dict(good, verdict="evil"))
    entries.append(dict(good, lux_avg=float("nan")))
    entries.append("not-a-dict")
    out = clean_stored_surveys(entries)
    assert len(out) == MAX_SURVEYS_KEPT
    assert out[0]["ts"] > out[-1]["ts"]  # newest-first
    assert clean_stored_surveys(None) == ()
    assert clean_stored_surveys("junk") == ()


# ════════════════════════════════════════════════════════════════════
# PlantRecord v1.35 fields
# ════════════════════════════════════════════════════════════════════


def _plant(**kw):
    base = dict(
        id="abc123",
        name="Citrus",
        wucols_category="moderate",
        canopy_area_sqft=50.0,
        zone_entity_id="switch.citrus",
    )
    base.update(kw)
    return PlantRecord(**base)


def test_plant_pre_v135_record_loads_with_defaults():
    rec = PlantRecord.from_dict(
        {
            "id": "abc123",
            "name": "Citrus",
            "wucols_category": "moderate",
            "canopy_area_sqft": 50.0,
            "zone_entity_id": "switch.citrus",
        }
    )
    assert rec.species == ""
    assert rec.lux_low is None and rec.lux_high is None
    assert rec.light_surveys == ()


def test_plant_lux_range_both_or_neither():
    with pytest.raises(ValueError):
        _plant(lux_low=100)
    with pytest.raises(ValueError):
        _plant(lux_high=100)
    p = _plant(lux_low=430, lux_high=1184)
    assert (p.lux_low, p.lux_high) == (430, 1184)


def test_plant_from_dict_malformed_lux_degrades_to_none():
    d = _plant().to_dict()
    d["lux_low"], d["lux_high"] = 900, 100  # inverted
    rec = PlantRecord.from_dict(d)
    assert rec.lux_low is None and rec.lux_high is None


def test_plant_species_roundtrip_and_truncation():
    p = _plant(species="Citrus sinensis")
    assert PlantRecord.from_dict(p.to_dict()).species == "Citrus sinensis"
    d = p.to_dict()
    d["species"] = "x" * 500
    assert len(PlantRecord.from_dict(d).species) == 120


def test_plant_light_surveys_cleaned_on_load():
    d = _plant().to_dict()
    d["light_surveys"] = [{"ts": "evil"}, 42]
    assert PlantRecord.from_dict(d).light_surveys == ()


# ════════════════════════════════════════════════════════════════════
# care_tasks
# ════════════════════════════════════════════════════════════════════


def _task(**kw):
    base = dict(id="t1", kind="fertilize", interval_days=90, plant_id="abc123")
    base.update(kw)
    return CareTask(**base)


def test_care_task_validation():
    with pytest.raises(ValueError):
        _task(kind="water")  # watering is not a care task
    with pytest.raises(ValueError):
        _task(interval_days=0)
    with pytest.raises(ValueError):
        _task(plant_id="", zone_entity_id="")  # needs a subject
    with pytest.raises(ValueError):
        CareTask(id="t2", kind="custom", interval_days=30, plant_id="p")  # custom needs label
    with pytest.raises(ValueError):
        _task(id="../evil")


def test_care_task_due_math():
    now = int(NOW.timestamp())
    never_done = _task()
    assert never_done.next_due_ts() == 0
    assert never_done.is_due(now)
    done_recently = _task(last_done_ts=now - 10 * 86400)
    assert not done_recently.is_due(now)  # 10 of 90 days elapsed
    overdue = _task(last_done_ts=now - 91 * 86400)
    assert overdue.is_due(now)


def test_care_task_reminder_renags_weekly_not_every_tick():
    now = int(NOW.timestamp())
    t = _task(last_done_ts=now - 91 * 86400)
    assert t.needs_reminder(now)
    notified = t.notified(now)
    assert not notified.needs_reminder(now + 3600)  # an hour later: no re-nag
    assert notified.needs_reminder(now + 8 * 86400)  # a week+ later: nag again


def test_care_task_completed_rearms():
    now = int(NOW.timestamp())
    t = _task(last_done_ts=now - 91 * 86400, last_notified_ts=now)
    done = t.completed(now)
    assert done.last_done_ts == now
    assert done.last_notified_ts is None
    assert not done.is_due(now)


def test_care_task_disabled_never_due():
    now = int(NOW.timestamp())
    assert not _task(enabled=False).is_due(now)


def test_care_store_roundtrip_and_corrupt_skip():
    store = CareTaskStore()
    store.add(_task())
    store.add(_task(id="t2", kind="prune", zone_entity_id="switch.trees", plant_id=""))
    data = store.to_serializable()
    data.append({"id": "bad", "kind": "evil"})
    loaded = CareTaskStore.from_serializable(data)
    assert {t.id for t in loaded.all()} == {"t1", "t2"}
    assert loaded.get("t1").kind == "fertilize"


# ════════════════════════════════════════════════════════════════════
# watering_diagnosis
# ════════════════════════════════════════════════════════════════════


def _run(status, days_ago, reason=""):
    return RunHistoryRecord(
        id=f"r{days_ago}{status}",
        started_at=(NOW - timedelta(days=days_ago)).isoformat(),
        zone_entity_id="switch.citrus",
        zone_name="Citrus",
        requested_minutes=30,
        status=status,
        source="schedule",
        reason=reason,
    )


def _diag(current, history=None, concerns=None, **kw):
    args = dict(
        zone_entity_id="switch.citrus",
        now=NOW,
        current_pct=current,
        min_pct=21.0,
        target_pct=31.0,
        max_pct=45.0,
        history=history or [],
        plant_concerns=concerns,
    )
    args.update(kw)
    return diagnose_zone(**args)


def test_diagnosis_no_reading_is_unknown():
    d = _diag(None)
    assert d.status == STATUS_UNKNOWN
    assert d.confirm  # tells the user to bind a sensor


def test_diagnosis_overwatering_at_max():
    d = _diag(50.0)
    assert d.status == STATUS_POSSIBLE_OVER
    assert d.signs and d.confirm and d.suggestions


def test_diagnosis_overwatering_from_repeated_moisture_skips():
    hist = [_run(STATUS_SKIPPED, i, reason="soil already moist") for i in (1, 3, 5)]
    d = _diag(35.0, history=hist)  # between target and max
    assert d.status == STATUS_POSSIBLE_OVER


def test_diagnosis_underwatering_below_min_despite_runs():
    hist = [_run(STATUS_COMPLETED, i) for i in (1, 3, 5, 7)]
    d = _diag(12.0, history=hist)
    assert d.status == STATUS_POSSIBLE_UNDER
    assert any("despite" in s for s in d.signs)


def test_diagnosis_aborted_runs_corroborate_under():
    hist = [_run(STATUS_ABORTED, 1), _run(STATUS_ABORTED, 2)]
    d = _diag(15.0, history=hist)
    assert d.status == STATUS_POSSIBLE_UNDER
    assert any("cut short" in s for s in d.signs)


def test_diagnosis_keywords_alone_never_trip_a_verdict():
    # Healthy moisture + scary vision keywords -> still OK (soil outranks words).
    d = _diag(30.0, concerns=["leaves yellowing badly", "wilting stems"])
    assert d.status == STATUS_OK


def test_diagnosis_keywords_corroborate_when_soil_agrees():
    d = _diag(50.0, concerns=["lower leaves yellowing"])
    assert d.status == STATUS_POSSIBLE_OVER
    assert any("Vision health" in s for s in d.signs)


def test_diagnosis_ok_zone():
    d = _diag(30.0, history=[_run(STATUS_COMPLETED, 2)])
    assert d.status == STATUS_OK
    assert d.signs == ()


def test_diagnosis_old_history_ignored():
    hist = [_run(STATUS_SKIPPED, 30, reason="soil already moist") for _ in range(5)]
    d = _diag(35.0, history=hist)  # skips are outside the 14-day lookback
    assert d.status == STATUS_OK


def test_diagnosis_serializes():
    d = _diag(50.0)
    out = d.to_dict()
    assert out["status"] == STATUS_POSSIBLE_OVER
    assert isinstance(out["signs"], list) and isinstance(out["evidence"], dict)


# ════════════════════════════════════════════════════════════════════
# yard_map safe_export_size (v1.35.1 — Esri cache-scale floor)
# ════════════════════════════════════════════════════════════════════


def test_safe_export_size_shrinks_sharp_requests():
    from custom_components.complete_irrigation.yard_map import (
        MIN_EXPORT_M_PER_PX,
        safe_export_size,
    )

    # A 60 m yard at 1536 px would be 0.04 m/px — Esri 500s; must shrink to >= 0.3.
    fw, fh = safe_export_size(60.0, 1536, 1282)
    assert max(fw, fh) == int(60.0 / MIN_EXPORT_M_PER_PX)  # 200 px
    assert 60.0 / max(fw, fh) >= MIN_EXPORT_M_PER_PX - 1e-9
    # Aspect preserved (within rounding).
    assert abs(fw / fh - 1536 / 1282) < 0.02


def test_safe_export_size_passes_coarse_requests_through():
    from custom_components.complete_irrigation.yard_map import safe_export_size

    # 600 m at 1536 px = 0.39 m/px — already coarser than the floor: unchanged.
    assert safe_export_size(600.0, 1536, 1282) == (1536, 1282)


def test_safe_export_size_degenerate_inputs():
    from custom_components.complete_irrigation.yard_map import safe_export_size

    assert safe_export_size(0.0, 1536, 1282) == (1536, 1282)
    assert safe_export_size(60.0, 0, 0) == (0, 0)
    assert safe_export_size(60.0, 1, 1) == (1, 1)  # tiny request stays tiny


# ════════════════════════════════════════════════════════════════════
# v1.36 — light-verdict stress triangulation + care-plan seeding
# ════════════════════════════════════════════════════════════════════


def test_diagnosis_light_verdict_corroborates_but_never_trips():
    # Healthy soil + too_high light -> still OK (light alone never trips).
    d = _diag(30.0, light_verdicts=[{"plant": "Citrus", "verdict": "too_high"}])
    assert d.status == STATUS_OK
    # Dry soil + too_high light -> under verdict WITH the light sign attached.
    hist = [_run(STATUS_COMPLETED, i) for i in (1, 3, 5)]
    d = _diag(12.0, history=hist, light_verdicts=[{"plant": "Citrus", "verdict": "too_high"}])
    assert d.status == STATUS_POSSIBLE_UNDER
    assert any("MORE light" in s for s in d.signs)
    assert d.evidence["light_verdicts"] == [{"plant": "Citrus", "verdict": "too_high"}]


def test_diagnosis_low_light_corroborates_overwatering():
    d = _diag(50.0, light_verdicts=[{"plant": "Fern", "verdict": "too_low"}])
    assert d.status == STATUS_POSSIBLE_OVER
    assert any("LESS light" in s for s in d.signs)


def test_diagnosis_optimal_and_junk_light_verdicts_ignored():
    d = _diag(
        30.0,
        light_verdicts=[
            {"plant": "A", "verdict": "optimal"},
            {"plant": "B", "verdict": "evil"},
            "junk",
        ],
    )
    assert d.status == STATUS_OK
    assert d.evidence["light_verdicts"] == []


def test_seed_care_plan_presets():
    from custom_components.complete_irrigation.care_tasks import (
        CARE_PLAN_PRESETS,
        seed_care_plan,
    )

    counter = iter(range(100))
    tasks = seed_care_plan("p1", "tree", id_factory=lambda: f"id{next(counter)}")
    assert [(t.kind, t.interval_days) for t in tasks] == list(CARE_PLAN_PRESETS["tree"])
    assert all(t.plant_id == "p1" for t in tasks)
    assert len({t.id for t in tasks}) == len(tasks)  # unique ids
    with pytest.raises(ValueError):
        seed_care_plan("p1", "bonsai", id_factory=lambda: "x")


# ════════════════════════════════════════════════════════════════════
# v1.37 — species_id rail
# ════════════════════════════════════════════════════════════════════


def test_species_suggestion_bounds_everything():
    from custom_components.complete_irrigation.species_id import validate_suggestion

    raw = {
        "species": "Pittosporum tobira" + "\x00" * 5,
        "common_name": "Japanese pittosporum",
        "confidence": 0.92,
        "sunlight_class": "full_sun",
        "temp_low_f": 23,
        "temp_high_f": 106,
        "wucols_category": "moderate",
        "care_plan_preset": "shrub",
        "water_every_days": 1,
        "fertilize_every_days": 0,
        "note": "n" * 999,
    }
    out = validate_suggestion(raw, model="qwen2.5vl", now_iso="2026-07-09T00:00:00Z")
    assert out["species"] == "Pittosporum tobira"
    assert out["sunlight_class"] == "full_sun"
    assert (out["temp_low_f"], out["temp_high_f"]) == (23, 106)
    assert out["fertilize_every_days"] == 0
    assert len(out["note"]) == 240


def test_species_suggestion_rejects_junk():
    from custom_components.complete_irrigation.species_id import validate_suggestion

    assert validate_suggestion(None, model="m", now_iso="t") is None
    assert validate_suggestion({"species": ""}, model="m", now_iso="t") is None
    out = validate_suggestion(
        {
            "species": "X",
            "confidence": float("nan"),
            "sunlight_class": "vantablack",
            "temp_low_f": 200,
            "temp_high_f": -100,
            "wucols_category": "soggy",
            "care_plan_preset": "bonsai",
            "water_every_days": 0,
        },
        model="m",
        now_iso="t",
    )
    assert out["confidence"] == 0.5  # NaN -> default
    assert out["sunlight_class"] is None
    assert out["temp_low_f"] is None and out["temp_high_f"] is None
    assert out["wucols_category"] is None and out["care_plan_preset"] is None
    assert out["water_every_days"] is None


def test_species_suggestion_lux_mapping_and_plant_roundtrip():
    from custom_components.complete_irrigation.species_id import (
        SUNLIGHT_LUX,
        suggested_lux_range,
        validate_suggestion,
    )

    sug = validate_suggestion(
        {"species": "X", "sunlight_class": "bright_shade"}, model="m", now_iso="t"
    )
    assert suggested_lux_range(sug) == SUNLIGHT_LUX["bright_shade"]
    assert suggested_lux_range({"sunlight_class": None}) is None
    # PlantRecord stores + reloads the suggestion through the load-path cleaner.
    p = _plant(species_suggestion=sug)
    rec = PlantRecord.from_dict(p.to_dict())
    assert rec.species_suggestion["species"] == "X"
    d = p.to_dict()
    d["species_suggestion"] = {"species": ""}  # junk degrades to None
    assert PlantRecord.from_dict(d).species_suggestion is None


def test_species_suggestion_unhashable_enums_degrade_not_crash():
    """v1.37 gate blocker: list/dict enum values must degrade to None (a TypeError
    on load would drop the WHOLE plant record -> watering-affecting rail violation)."""
    from custom_components.complete_irrigation.species_id import (
        clean_stored_suggestion,
        validate_suggestion,
    )

    raw = {"species": "Rose", "sunlight_class": ["full_sun"], "wucols_category": {"a": 1}}
    out = validate_suggestion(raw, model="m", now_iso="t")
    assert out["sunlight_class"] is None and out["wucols_category"] is None
    assert clean_stored_suggestion({"species": "x", "care_plan_preset": ["tree"]}) is not None
    # And the whole plant record survives a poisoned stored suggestion.
    d = _plant().to_dict()
    d["species_suggestion"] = {"species": "x", "sunlight_class": []}
    rec = PlantRecord.from_dict(d)
    assert rec.name == "Citrus"


def test_species_suggestion_strips_bidi_and_c1_controls():
    from custom_components.complete_irrigation.species_id import _clean_text

    assert "‮" not in _clean_text("evil‮gnp", 120)
    assert "\x85" not in _clean_text("a\x85b", 120)
    assert "\x1b" not in _clean_text("2026\x1b]0;x\x07", 40)
