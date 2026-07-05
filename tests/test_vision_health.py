"""Tests for the plant vision-health core (pure logic, v1.33).

Photo-pair selection + the deterministic RAIL that bounds a local vision model's
verdict — the safety-critical part: a hallucinating/malformed model can never inject
an actuation instruction as "care", store unbounded garbage, or drive watering.
"""

from __future__ import annotations

import json

from custom_components.complete_irrigation.vision_health import (
    HEALTH_STATES,
    HealthReport,
    due_for_review,
    report_has_concern,
    select_review_photos,
    serialize_report,
    validate_verdict,
)

DAY = 86400
NOW_ISO = "2026-05-18T06:00:00+00:00"


def _photo(ts, path=None):
    return {"ts": ts, "path": path or f"/local/complete_irrigation/plants/p1/{ts}.jpg"}


# ── select_review_photos ────────────────────────────────────────────


def test_select_none_when_empty():
    assert select_review_photos([]) is None
    assert select_review_photos(None) is None


def test_select_latest_only_when_single_photo():
    sel = select_review_photos([_photo(1000)])
    assert sel["latest"]["ts"] == 1000
    assert sel["baseline"] is None


def test_select_baseline_when_gap_met():
    photos = [_photo(200 * DAY), _photo(100 * DAY), _photo(10 * DAY)]  # newest-first-ish
    sel = select_review_photos(photos, min_gap_days=60)
    assert sel["latest"]["ts"] == 200 * DAY
    assert sel["baseline"]["ts"] == 10 * DAY  # oldest, gap >= 60d


def test_select_no_baseline_when_gap_too_small():
    photos = [_photo(100 * DAY), _photo(90 * DAY)]  # only 10 days apart
    sel = select_review_photos(photos, min_gap_days=60)
    assert sel["baseline"] is None


def test_select_filters_malformed_entries():
    photos = [_photo(100 * DAY), {"ts": "bad"}, "junk", {"path": "/local/x.jpg"}, _photo(10 * DAY)]
    sel = select_review_photos(photos, min_gap_days=60)
    assert sel["latest"]["ts"] == 100 * DAY
    assert sel["baseline"]["ts"] == 10 * DAY


def test_select_sorts_unordered_input():
    sel = select_review_photos([_photo(50 * DAY), _photo(300 * DAY), _photo(1 * DAY)])
    assert sel["latest"]["ts"] == 300 * DAY
    assert sel["baseline"]["ts"] == 1 * DAY


# ── validate_verdict: the RAIL ──────────────────────────────────────


def _valid_raw(**over):
    raw = {
        "health_state": "healthy",
        "confidence": 0.9,
        "changes_since_last": "New growth, greener canopy.",
        "concerns": [],
        "suggested_care": ["Keep current watering."],
    }
    raw.update(over)
    return raw


def _validate(raw):
    return validate_verdict(
        raw,
        plant_id="p1",
        latest_ts=200 * DAY,
        baseline_ts=10 * DAY,
        model="qwen2.5-vl",
        now_iso=NOW_ISO,
    )


def test_validate_happy_path():
    r = _validate(_valid_raw())
    assert isinstance(r, HealthReport)
    assert r.health_state == "healthy"
    assert r.confidence == 0.9
    assert r.plant_id == "p1"
    assert r.latest_ts == 200 * DAY and r.baseline_ts == 10 * DAY
    assert r.model == "qwen2.5-vl"


def test_validate_non_dict_is_none():
    for bad in (None, "text", 42, ["list"]):
        assert _validate(bad) is None


def test_out_of_enum_state_becomes_unknown():
    assert _validate(_valid_raw(health_state="apocalyptic")).health_state == "unknown"
    assert _validate(_valid_raw(health_state=123)).health_state == "unknown"
    # every allowed state passes through
    for s in HEALTH_STATES:
        assert _validate(_valid_raw(health_state=s.upper())).health_state == s


def test_confidence_clamped_and_defaulted():
    assert _validate(_valid_raw(confidence=5)).confidence == 1.0
    assert _validate(_valid_raw(confidence=-2)).confidence == 0.0
    assert _validate(_valid_raw(confidence="nan-ish")).confidence == 0.5  # bad -> default


def test_confidence_nan_inf_defaulted_not_stored():
    # A NaN/Inf confidence must NOT survive (json.dumps(NaN) is invalid JSON and would
    # corrupt the .storage write). It falls back to the default and stays JSON-safe.
    import math

    for bad in (float("nan"), float("inf"), float("-inf")):
        r = _validate(_valid_raw(confidence=bad))
        assert math.isfinite(r.confidence)
        assert r.confidence == 0.5
        json.dumps(serialize_report(r))  # must not raise / emit NaN


def test_text_truncated_and_nonstring_dropped():
    r = _validate(_valid_raw(changes_since_last="x" * 5000))
    assert len(r.changes_since_last) <= 600
    assert _validate(_valid_raw(changes_since_last={"not": "a string"})).changes_since_last == ""


def test_lists_capped():
    r = _validate(_valid_raw(concerns=[f"c{i}" for i in range(50)]))
    assert len(r.concerns) <= 6


def test_rail_strips_actuation_disguised_as_care():
    # THE SAFETY RAIL: a hallucinating model must not surface control instructions.
    raw = _valid_raw(
        suggested_care=[
            "Water a little more in summer.",  # legit -> kept
            "Turn on switch.citrus for 20 minutes.",  # actuation -> dropped
            "call complete_irrigation.run_zone",  # service call -> dropped
            "Activate the zone now.",  # actuation -> dropped
            "Move it to more shade.",  # legit -> kept
        ]
    )
    care = _validate(raw).suggested_care
    assert "Water a little more in summer." in care
    assert "Move it to more shade." in care
    assert not any("switch." in c or "run_zone" in c or "Activate" in c for c in care)


def test_rail_strips_actuation_in_concerns_too():
    r = _validate(_valid_raw(concerns=["Leaves yellowing.", "turn off the water supply"]))
    assert "Leaves yellowing." in r.concerns
    assert not any("turn off" in c.lower() for c in r.concerns)


def test_rail_broadened_verbs_and_homoglyphs():
    # v1.33 polish: broadened verb/noun set + NFKC normalization so more actuation
    # phrasings and fullwidth/homoglyph evasions are dropped. Legit prose is kept.
    care = _validate(
        _valid_raw(
            suggested_care=[
                "Give it more morning sun.",  # legit -> kept
                "Open the valve for an hour.",  # actuation -> dropped
                "Enable the schedule today.",  # actuation -> dropped
                "Trigger the irrigation cycle.",  # actuation -> dropped
                "Press the run button.",  # actuation -> dropped
                "Turn on switch．citrus now.",  # noqa: RUF001  # homoglyph switch. -> dropped
                "Prune the dead tips.",  # legit -> kept
            ]
        )
    ).suggested_care
    assert care == ("Give it more morning sun.", "Prune the dead tips.")


# ── due_for_review ──────────────────────────────────────────────────


def test_due_for_review():
    # never assessed -> due
    assert due_for_review("", NOW_ISO) is True
    assert due_for_review(None, NOW_ISO) is True
    # unparseable / tz-naive -> due (fail toward reviewing)
    assert due_for_review("garbage", NOW_ISO) is True
    assert due_for_review("2026-05-01T06:00:00", NOW_ISO) is True  # naive
    # recent (17 days ago) with a 182-day interval -> not due
    assert due_for_review("2026-05-01T06:00:00+00:00", NOW_ISO, interval_days=182) is False
    # old (>182 days) -> due
    assert due_for_review("2025-01-01T06:00:00+00:00", NOW_ISO, interval_days=182) is True


# ── concern flag + serialization ────────────────────────────────────


def test_report_has_concern():
    assert report_has_concern(_validate(_valid_raw(health_state="declining")))
    assert report_has_concern(_validate(_valid_raw(concerns=["Wilting"])))
    assert not report_has_concern(_validate(_valid_raw(health_state="healthy", concerns=[])))


def test_serialize_is_json_safe():
    out = serialize_report(_validate(_valid_raw(health_state="stressed", concerns=["Dry tips"])))
    assert out["health_state"] == "stressed"
    assert out["needs_attention"] is True
    assert isinstance(out["concerns"], list)
    json.dumps(out)  # must not raise
