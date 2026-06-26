"""v1.30 restart fail-over — pure resume-planning logic (plan_session_resume).

On an HA restart the in-memory auto-stop / block timers are gone, but each
in-progress run persists an "active session" with its planned end. This decides,
per session, whether to RESUME the remaining time or CLOSE it out (finished during
the outage / bad data). HA-free, so it's unit-tested without standing up HA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.complete_irrigation.coordinator_logic import plan_session_resume

NOW = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)


def _session(deadline, total=None):
    s = {"deadline": deadline.isoformat()}
    if total is not None:
        s["total_minutes"] = total
    return s


def test_resumes_run_still_within_its_window():
    # Planned end is 40 min out -> resume for ~40 more minutes.
    sessions = {"switch.citrus": _session(NOW + timedelta(minutes=40), total=240)}
    plan = plan_session_resume(sessions, NOW)
    assert len(plan) == 1
    assert plan[0]["zone"] == "switch.citrus"
    assert plan[0]["action"] == "resume"
    assert plan[0]["remaining_minutes"] == 40


def test_closes_run_past_its_planned_end():
    # Planned end was 20 min ago -> the run finished during the outage; close it.
    sessions = {"switch.birdbath": _session(NOW - timedelta(minutes=20))}
    plan = plan_session_resume(sessions, NOW)
    assert plan[0]["action"] == "close"
    assert plan[0]["remaining_minutes"] == 0


def test_closes_when_barely_any_time_left():
    # < min_resume_seconds remaining -> not worth resuming.
    sessions = {"switch.z": _session(NOW + timedelta(seconds=30))}
    plan = plan_session_resume(sessions, NOW)
    assert plan[0]["action"] == "close"


def test_remaining_capped_at_total_minutes():
    # Clock skew could make the deadline look far away; never resume longer
    # than the run's own total.
    sessions = {"switch.z": _session(NOW + timedelta(minutes=999), total=58)}
    plan = plan_session_resume(sessions, NOW)
    assert plan[0]["action"] == "resume"
    assert plan[0]["remaining_minutes"] == 58


def test_bad_deadline_is_closed_not_resumed():
    # Missing / unparseable deadline must fail SAFE (close + valve off), never resume.
    for bad in ({}, {"deadline": "not-a-date"}, {"deadline": None}, None):
        plan = plan_session_resume({"switch.z": bad}, NOW)
        assert plan[0]["action"] == "close"


def test_naive_vs_aware_mismatch_is_closed():
    # A tz mismatch between the stored deadline and `now` must not crash; close.
    naive = {"deadline": datetime(2026, 6, 25, 10, 0, 0).isoformat()}  # no tz
    plan = plan_session_resume({"switch.z": naive}, NOW)  # NOW is tz-aware
    assert plan[0]["action"] == "close"


def test_multiple_sessions_each_decided():
    sessions = {
        "switch.a": _session(NOW + timedelta(minutes=30), total=120),
        "switch.b": _session(NOW - timedelta(minutes=5)),
    }
    plan = {p["zone"]: p for p in plan_session_resume(sessions, NOW)}
    assert plan["switch.a"]["action"] == "resume"
    assert plan["switch.b"]["action"] == "close"


def test_empty_sessions_is_empty_plan():
    assert plan_session_resume({}, NOW) == []


def test_resume_prefers_gap_free_water_deadline():
    # deadline (gap-inclusive) is further out than water_deadline (gaps-free);
    # resume must use the WATER deadline so re-added gaps don't become water.
    sessions = {
        "switch.citrus": {
            "deadline": (NOW + timedelta(minutes=42)).isoformat(),  # +2 min of gaps
            "water_deadline": (NOW + timedelta(minutes=40)).isoformat(),
            "total_minutes": 240,
        }
    }
    plan = plan_session_resume(sessions, NOW)
    assert plan[0]["action"] == "resume"
    assert plan[0]["remaining_minutes"] == 40  # water, not 42


def test_falls_back_to_deadline_when_no_water_deadline():
    # Pre-fix / single-run sessions have only `deadline`.
    sessions = {"switch.z": {"deadline": (NOW + timedelta(minutes=15)).isoformat()}}
    plan = plan_session_resume(sessions, NOW)
    assert plan[0]["action"] == "resume"
    assert plan[0]["remaining_minutes"] == 15
