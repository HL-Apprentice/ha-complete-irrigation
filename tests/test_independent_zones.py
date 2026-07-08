"""Independent-supply zones + orphaned-session self-heal.

A zone on its OWN water line (e.g. a birdbath fill that does not share the drip
manifold's pressure) must be exempt from one-zone-at-a-time serialization: it fires at
its scheduled time, is never deferred/compressed by other zones, and never blocks them.

And an orphaned session (a chunked run interrupted before its final block never cleared
its record) must be pruned once its window has fully elapsed, so it can't phantom-reserve
the controller across a restart — the failure that silently dropped the midday birdbath
runs after an app update restarted HA mid-sequence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.complete_irrigation.conflict_resolver import (
    POLICY_DEFER_NEW,
    REASON_COMMITTED,
    committed_runs_from_sessions,
    resolve_conflicts,
    stale_session_zones,
)
from custom_components.complete_irrigation.run_planner import PlannedRun

NOW = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
BIRD = "switch.bird_bath"
CITRUS = "switch.citrus"


def _run(zone, start_min_from_now, dur, sid, reason="scheduled"):
    return PlannedRun(
        zone_entity_id=zone,
        start_at=NOW + timedelta(minutes=start_min_from_now),
        duration_minutes=dur,
        schedule_id=sid,
        schedule_name=sid.upper(),
        reason=reason,
    )


def _session(started_min_before, total_minutes, *, sid="citrus"):
    started = NOW - timedelta(minutes=started_min_before)
    deadline = started + timedelta(minutes=total_minutes)
    return {
        "total_minutes": total_minutes,
        "started_at": started.isoformat(),
        "deadline": deadline.isoformat(),
        "water_deadline": deadline.isoformat(),
        "schedule_id": sid,
        "schedule_name": sid.upper(),
        "source": "scheduled",
    }


# ── resolve_conflicts: the birdbath never defers ────────────────────────────


def test_independent_run_fires_on_time_over_a_committed_blocker():
    """The birdbath overlaps a live 4-hour citrus run, but on its own line it must fire
    at its scheduled time — NOT be deferred behind citrus (the actual production bug)."""
    citrus = _run(CITRUS, -2, 240, "citrus", reason=REASON_COMMITTED)  # live, blocks the controller
    bird = _run(BIRD, 0, 15, "birdbath")  # scheduled now, inside citrus's window
    out = resolve_conflicts([citrus, bird], POLICY_DEFER_NEW, independent_zones=frozenset({BIRD}))
    bird_out = [r for r in out if r.zone_entity_id == BIRD]
    assert len(bird_out) == 1
    assert bird_out[0].start_at == NOW  # fired on time, not deferred
    assert bird_out[0].duration_minutes == 15


def test_without_exemption_the_birdbath_is_deferred():
    """Control: with NO exemption the same birdbath is pushed past the live citrus run —
    proving the exemption is what changes the outcome."""
    citrus = _run(CITRUS, -2, 240, "citrus", reason=REASON_COMMITTED)
    bird = _run(BIRD, 0, 15, "birdbath")
    out = resolve_conflicts([citrus, bird], POLICY_DEFER_NEW)  # no independent_zones
    bird_out = [r for r in out if r.zone_entity_id == BIRD]
    assert bird_out[0].start_at > NOW  # deferred behind citrus


def test_independent_zone_does_not_block_a_shared_zone():
    """A shared zone scheduled during the birdbath's run must NOT be deferred by it."""
    bird = _run(BIRD, 0, 60, "birdbath")
    grass = _run("switch.grass", 5, 20, "grass")
    out = resolve_conflicts([bird, grass], POLICY_DEFER_NEW, independent_zones=frozenset({BIRD}))
    grass_out = [r for r in out if r.zone_entity_id == "switch.grass"]
    assert grass_out[0].start_at == NOW + timedelta(minutes=5)  # on time, unblocked


def test_independent_session_is_not_a_committed_blocker():
    sessions = {BIRD: _session(2, 15, sid="birdbath"), CITRUS: _session(2, 240, sid="citrus")}
    out = committed_runs_from_sessions(sessions, NOW, independent_zones=frozenset({BIRD}))
    zones = {r.zone_entity_id for r in out}
    assert BIRD not in zones  # birdbath session never reserves the controller
    assert CITRUS in zones


# ── stale_session_zones: orphan self-heal ──────────────────────────────────


def test_expired_session_is_reported_stale():
    # started 5h ago, ran 60m -> deadline 4h in the past -> well past deadline+buffer
    sessions = {CITRUS: _session(300, 60, sid="citrus")}
    assert stale_session_zones(sessions, NOW) == [CITRUS]


def test_fresh_session_is_not_stale():
    # started 2m ago, 240m run -> deadline far in the future
    sessions = {CITRUS: _session(2, 240, sid="citrus")}
    assert stale_session_zones(sessions, NOW) == []


def test_malformed_session_is_left_alone():
    # No parseable deadline -> conservatively NOT pruned (still ignored as a blocker).
    sessions = {"switch.x": {"started_at": "not-a-date"}}
    assert stale_session_zones(sessions, NOW) == []
