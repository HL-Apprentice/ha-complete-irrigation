"""v1.39 — gap-aware short-run insertion (gap_insertion.py, pure logic).

A chunked long run's inter-block gaps are controller-IDLE, but the resolver
blocks the whole gap-inclusive span — so a 15-min zone on the shared manifold
used to defer for hours behind a 5-block run. plan_gap_insertions pins a short
SCHEDULED run inside a gap when it fits entirely, with a safety margin on both
sides; everything that doesn't fit defers exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.complete_irrigation.chunked_run import plan_blocks
from custom_components.complete_irrigation.conflict_resolver import (
    POLICY_DEFER_NEW,
    REASON_COMMITTED,
    committed_runs_from_sessions,
    resolve_conflicts,
)
from custom_components.complete_irrigation.gap_insertion import (
    DEFAULT_FIRE_LATENCY_SECONDS,
    REASON_GAP_INSERTED,
    GapWindow,
    effective_margin_seconds,
    insertion_key,
    insertion_start_in_gap,
    plan_gap_insertions,
    session_gap_windows,
)
from custom_components.complete_irrigation.run_planner import PlannedRun, due_runs_since
from custom_components.complete_irrigation.schedule import DEFAULT_ZONE_BUFFER_SECONDS

NOW = datetime(2026, 7, 8, 6, 30, tzinfo=UTC)
CITRUS = "switch.citrus"
GARDEN = "switch.garden"
BIRD = "switch.bird_bath"

# Default margin: valve-settle buffer (30s) + one tick of fire latency (60s).
MARGIN_S = DEFAULT_ZONE_BUFFER_SECONDS + DEFAULT_FIRE_LATENCY_SECONDS


def _session(zone_start, total_minutes, *, cap=58, gap_s=1200, sid="citrus", **extra):
    """A session record exactly as services._record_active_session writes it
    for a chunked run (block plan included)."""
    blocks = plan_blocks(total_minutes, cap)
    extra_s = (len(blocks) - 1) * gap_s
    s = {
        "total_minutes": total_minutes,
        "started_at": zone_start.isoformat(),
        "deadline": (zone_start + timedelta(minutes=total_minutes, seconds=extra_s)).isoformat(),
        "water_deadline": (zone_start + timedelta(minutes=total_minutes)).isoformat(),
        "schedule_id": sid,
        "schedule_name": (sid or "").upper(),
        "source": "scheduled",
        "blocks": blocks,
        "block_gap_seconds": gap_s,
    }
    s.update(extra)
    return s


def _run(zone, start_at, dur, sid, reason="scheduled"):
    return PlannedRun(
        zone_entity_id=zone,
        start_at=start_at,
        duration_minutes=dur,
        schedule_id=sid,
        schedule_name=sid.upper(),
        reason=reason,
    )


# ── session_gap_windows: exact reconstruction of the block timers ──────────


def test_gap_windows_match_dispatch_offsets():
    # 280 min at cap 58 -> [58, 58, 58, 58, 48]: 4 gaps, at the exact offsets
    # services._dispatch_run_blocks arms its timers with.
    start = NOW - timedelta(minutes=30)
    gaps = session_gap_windows(CITRUS, _session(start, 280, gap_s=1200))
    assert len(gaps) == 4
    cursor = start
    for g, block in zip(gaps, [58, 58, 58, 58], strict=True):
        assert g.zone_entity_id == CITRUS
        assert g.start == cursor + timedelta(minutes=block)
        assert g.end == g.start + timedelta(seconds=1200)
        assert g.seconds == 1200
        cursor = g.end


def test_single_block_session_has_no_gaps():
    gaps = session_gap_windows(CITRUS, _session(NOW, 45, cap=58))
    assert gaps == []


def test_malformed_sessions_expose_no_gaps():
    base = _session(NOW, 280)
    cases = [
        None,
        "junk",
        {},
        {**base, "blocks": None},  # no plan recorded (pre-v1.39 session)
        {k: v for k, v in base.items() if k != "blocks"},
        {**base, "blocks": [58]},  # one block = not chunked
        {**base, "blocks": [58, "x"]},  # garbage entry
        {**base, "blocks": [58, 0]},  # non-positive block
        {**base, "blocks": [58, True]},  # bool is not a duration
        {**base, "block_gap_seconds": 0},  # no gap to insert into
        {**base, "block_gap_seconds": -30},
        {**base, "block_gap_seconds": None},
        {**base, "started_at": "not-a-date"},
        {k: v for k, v in base.items() if k != "started_at"},
    ]
    for s in cases:
        assert session_gap_windows(CITRUS, s) == [], f"expected no gaps for {s!r}"


# ── margin + fit math ───────────────────────────────────────────────────────


def test_effective_margin_is_buffer_plus_tick():
    assert effective_margin_seconds() == DEFAULT_ZONE_BUFFER_SECONDS + 60
    assert effective_margin_seconds(zone_buffer_seconds=45) == 45 + 60
    assert effective_margin_seconds(zone_buffer_seconds=45, tick_seconds=30) == 75
    assert effective_margin_seconds(zone_buffer_seconds=-5) == 60  # clamped


def test_exactly_fitting_gap_is_accepted():
    # gap == run + 2*margin fits, pinned margin past the gap's opening.
    margin = timedelta(seconds=MARGIN_S)
    run_len = timedelta(minutes=15)
    gap = GapWindow(CITRUS, NOW, NOW + run_len + 2 * margin)
    pin = insertion_start_in_gap(gap, NOW - timedelta(hours=1), run_len, margin)
    assert pin == gap.start + margin
    assert pin + run_len + margin == gap.end  # exact fit, margins intact


def test_one_second_over_is_rejected():
    margin = timedelta(seconds=MARGIN_S)
    run_len = timedelta(minutes=15)
    gap = GapWindow(CITRUS, NOW, NOW + run_len + 2 * margin - timedelta(seconds=1))
    assert insertion_start_in_gap(gap, NOW - timedelta(hours=1), run_len, margin) is None


def test_pin_never_earlier_than_scheduled_start():
    # A run scheduled mid-gap starts AT its scheduled time (never earlier)…
    margin = timedelta(seconds=MARGIN_S)
    run_len = timedelta(minutes=5)
    gap = GapWindow(CITRUS, NOW, NOW + timedelta(minutes=20))
    sched = NOW + timedelta(minutes=3)
    pin = insertion_start_in_gap(gap, sched, run_len, margin)
    assert pin == sched
    # …and if that late start no longer leaves the tail margin, it's rejected.
    sched_late = gap.end - run_len - margin + timedelta(seconds=1)
    assert insertion_start_in_gap(gap, sched_late, run_len, margin) is None


# ── plan_gap_insertions: selection rules ────────────────────────────────────


def _citrus_state(started_min_ago=30, total=280, gap_s=1200):
    started = NOW - timedelta(minutes=started_min_ago)
    return started, {CITRUS: _session(started, total, gap_s=gap_s)}


def test_short_run_pinned_into_first_feasible_gap():
    _, sessions = _citrus_state()
    garden = _run(GARDEN, NOW, 15, "garden")  # due now, blocked by citrus
    plan = plan_gap_insertions([garden], sessions, NOW)
    key = insertion_key(garden)
    assert list(plan) == [key]
    gap1 = session_gap_windows(CITRUS, sessions[CITRUS])[0]
    assert plan[key] == gap1.start + timedelta(seconds=MARGIN_S)
    # The whole inserted interval + margins stays inside the gap.
    assert plan[key] + timedelta(minutes=15, seconds=MARGIN_S) <= gap1.end


def test_realistic_config_five_min_run_fits_600s_gap():
    # block_gap_seconds config maxes at 600 via update_settings; a 5-min zone
    # fits (300 + 2*90 = 480 <= 600).
    _, sessions = _citrus_state(gap_s=600)
    run = _run(GARDEN, NOW, 5, "garden")
    assert plan_gap_insertions([run], sessions, NOW)


def test_run_too_long_for_gap_defers_as_today():
    # 20 min + 2*90s = 1380s > 1200s gap -> no insertion anywhere.
    _, sessions = _citrus_state(gap_s=1200)
    run = _run(GARDEN, NOW, 20, "garden")
    assert plan_gap_insertions([run], sessions, NOW) == {}


def test_gap_exactly_fits_and_one_second_over_end_to_end():
    # 15-min run: required = 900 + 2*90 = 1080s.
    _, sessions_fit = _citrus_state(gap_s=1080)
    _, sessions_over = _citrus_state(gap_s=1079)
    run = _run(GARDEN, NOW, 15, "garden")
    assert plan_gap_insertions([run], sessions_fit, NOW)
    assert plan_gap_insertions([run], sessions_over, NOW) == {}


def test_only_plain_scheduled_runs_are_candidates():
    _, sessions = _citrus_state()
    for reason in (REASON_COMMITTED, REASON_GAP_INSERTED, "deferred", "compressed"):
        run = _run(GARDEN, NOW, 15, "garden", reason=reason)
        assert plan_gap_insertions([run], sessions, NOW) == {}, reason


def test_independent_zone_run_and_session_are_excluded():
    started, sessions = _citrus_state()
    bird = _run(BIRD, NOW, 15, "birdbath")
    # An independent-supply run never needs insertion (it never defers).
    assert plan_gap_insertions([bird], sessions, NOW, independent_zones=frozenset({BIRD})) == {}
    # An independent-supply SESSION exposes no gaps and blocks nothing.
    ind_sessions = {BIRD: _session(started, 280, sid="birdbath")}
    garden = _run(GARDEN, NOW, 15, "garden")
    assert (
        plan_gap_insertions([garden], ind_sessions, NOW, independent_zones=frozenset({BIRD})) == {}
    )


def test_unblocked_run_is_left_alone():
    # Scheduled after the whole citrus span — it fires on time as-is.
    started, sessions = _citrus_state(started_min_ago=0)
    after = started + timedelta(hours=7)
    run = _run(GARDEN, after, 15, "garden")
    assert plan_gap_insertions([run], sessions, NOW) == {}


def test_own_zone_session_disqualifies_the_run():
    # citrus's own future occurrence must never be "inserted" into its gaps.
    _, sessions = _citrus_state()
    own = _run(CITRUS, NOW + timedelta(minutes=5), 15, "citrus")
    assert plan_gap_insertions([own], sessions, NOW) == {}


def test_fired_ledger_prevents_reinsertion():
    _, sessions = _citrus_state()
    garden = _run(GARDEN, NOW, 15, "garden")
    key = insertion_key(garden)
    assert plan_gap_insertions([garden], sessions, NOW, fired_keys=frozenset({key})) == {}


def test_one_run_per_gap_two_shorts_get_two_gaps():
    _, sessions = _citrus_state()
    a = _run(GARDEN, NOW, 15, "garden")
    b = _run("switch.grass", NOW + timedelta(minutes=1), 15, "grass")
    plan = plan_gap_insertions([a, b], sessions, NOW)
    assert len(plan) == 2
    gaps = session_gap_windows(CITRUS, sessions[CITRUS])
    pa, pb = plan[insertion_key(a)], plan[insertion_key(b)]
    assert pa == gaps[0].start + timedelta(seconds=MARGIN_S)  # earliest run, earliest gap
    assert pb == gaps[1].start + timedelta(seconds=MARGIN_S)
    # Serialized: no overlap between the two inserted runs.
    assert pb >= pa + timedelta(minutes=15)


def test_past_gap_slot_is_skipped_for_a_later_gap():
    # Citrus started 80 min ago: gap 1 opened at +58 min and its pinned slot
    # (gap.start + margin) is ~20 min in the past — uncatchable. The run must
    # take gap 2, never a slot the due window can no longer reach.
    started, sessions = _citrus_state(started_min_ago=80)
    garden = _run(GARDEN, started + timedelta(minutes=5), 15, "garden")
    plan = plan_gap_insertions([garden], sessions, NOW)
    gaps = session_gap_windows(CITRUS, sessions[CITRUS])
    assert plan[insertion_key(garden)] == gaps[1].start + timedelta(seconds=MARGIN_S)


def test_just_passed_pin_is_still_catchable_within_one_tick():
    # A pin that slipped less than one tick into the past is exactly what the
    # margin's latency allowance covers — it must still be offered (the due
    # window (last, now] catches it this tick).
    started, sessions = _citrus_state(started_min_ago=0)
    gaps = session_gap_windows(CITRUS, sessions[CITRUS])
    pin = gaps[0].start + timedelta(seconds=MARGIN_S)
    garden = _run(GARDEN, started + timedelta(minutes=5), 15, "garden")
    now = pin + timedelta(seconds=59)  # pin is 59s past — inside one tick
    plan = plan_gap_insertions([garden], sessions, now)
    assert plan[insertion_key(garden)] == pin
    now_late = pin + timedelta(seconds=60)  # a full tick past — gone
    plan_late = plan_gap_insertions([garden], sessions, now_late)
    assert plan_late.get(insertion_key(garden)) != pin


def test_gap_overlapped_by_another_live_session_is_rejected():
    _, sessions = _citrus_state()
    gaps = session_gap_windows(CITRUS, sessions[CITRUS])
    # A (weird, but defensive) second live run covering gap 1 entirely.
    sessions["switch.other"] = {
        "total_minutes": 60,
        "started_at": (gaps[0].start - timedelta(minutes=5)).isoformat(),
        "deadline": (gaps[0].end + timedelta(minutes=5)).isoformat(),
        "water_deadline": (gaps[0].end + timedelta(minutes=5)).isoformat(),
        "schedule_id": "other",
        "schedule_name": "OTHER",
        "source": "manual",
    }
    garden = _run(GARDEN, NOW, 15, "garden")
    plan = plan_gap_insertions([garden], sessions, NOW)
    # Slotted into gap 2 instead — never into a window another zone occupies.
    assert plan[insertion_key(garden)] == gaps[1].start + timedelta(seconds=MARGIN_S)


def test_tz_mismatch_fails_safe():
    _, sessions = _citrus_state()
    naive_now = NOW.replace(tzinfo=None)
    garden = _run(GARDEN, NOW, 15, "garden")
    assert plan_gap_insertions([garden], sessions, naive_now) == {}


def test_zone_buffer_config_widens_the_margin():
    # margin = zb + tick; with zb=300 a 15-min run needs 900 + 2*360 = 1620s.
    _, sessions = _citrus_state(gap_s=1600)
    garden = _run(GARDEN, NOW, 15, "garden")
    assert plan_gap_insertions([garden], sessions, NOW, zone_buffer_seconds=300) == {}
    _, sessions_wide = _citrus_state(gap_s=1620)
    assert plan_gap_insertions([garden], sessions_wide, NOW, zone_buffer_seconds=300)


# ── resolver integration: pinned runs are FIXED and stay fireable ───────────


def test_resolver_keeps_inserted_run_pinned_and_fireable():
    _, sessions = _citrus_state()
    committed = committed_runs_from_sessions(sessions, NOW)
    garden = _run(GARDEN, NOW, 15, "garden")
    plan = plan_gap_insertions([garden], sessions, NOW)
    pin = plan[insertion_key(garden)]
    pinned = PlannedRun(
        zone_entity_id=GARDEN,
        start_at=pin,
        duration_minutes=15,
        schedule_id="garden",
        schedule_name="GARDEN",
        reason=REASON_GAP_INSERTED,
        scheduled_start_at=garden.start_at,
    )
    resolved = resolve_conflicts([*committed, pinned], POLICY_DEFER_NEW, compress_floor_pct=100)
    out = next(r for r in resolved if r.zone_entity_id == GARDEN)
    assert out.start_at == pin  # NOT pushed behind the chunked span
    assert out.reason == REASON_GAP_INSERTED
    assert out.duration_minutes == 15  # never compressed
    # The coordinator's post-resolve filter drops only REASON_COMMITTED, so
    # the pinned run still FIRES when its slot enters the due window.
    due = due_runs_since(resolved, pin - timedelta(seconds=30), pin + timedelta(seconds=30))
    fireable = [r for r in due if r.reason != REASON_COMMITTED]
    assert [r.zone_entity_id for r in fireable] == [GARDEN]


def test_other_runs_route_around_an_inserted_run():
    # A second adjustable run must treat the pinned interval as busy: it can
    # never be placed on top of it (it's pushed past the whole chunked span,
    # which contains the pin).
    _, sessions = _citrus_state()
    committed = committed_runs_from_sessions(sessions, NOW)
    pin = session_gap_windows(CITRUS, sessions[CITRUS])[0].start + timedelta(seconds=MARGIN_S)
    pinned = PlannedRun(
        zone_entity_id=GARDEN,
        start_at=pin,
        duration_minutes=15,
        schedule_id="garden",
        schedule_name="GARDEN",
        reason=REASON_GAP_INSERTED,
    )
    grass = _run("switch.grass", pin, 30, "grass")  # too long for any gap
    resolved = resolve_conflicts(
        [*committed, pinned, grass], POLICY_DEFER_NEW, compress_floor_pct=100
    )
    grass_out = next(r for r in resolved if r.schedule_id == "grass")
    pin_end = pin + timedelta(minutes=15)
    assert not (grass_out.start_at < pin_end and grass_out.start_at + timedelta(minutes=30) > pin)
    # In fact it lands past the whole gap-inclusive citrus span.
    citrus_end = datetime.fromisoformat(sessions[CITRUS]["deadline"])
    assert grass_out.start_at >= citrus_end


def test_insertion_key_survives_replace_and_deferral():
    from dataclasses import replace as dc_replace

    garden = _run(GARDEN, NOW, 15, "garden")
    moved = dc_replace(garden, start_at=NOW + timedelta(hours=2), reason=REASON_GAP_INSERTED)
    assert insertion_key(moved) == insertion_key(garden)
