"""Multi-tick simulation of the coordinator's plan->resolve->fire loop.

The coordinator recomputes the whole plan every 60s and fires via
due_runs_since((last, now]). A deferred/compressed run only fires if its
RESOLVED start lands inside one of those 60s windows. This test drives
that exact loop across a simulated day and asserts the bulletproof
guarantee: every scheduled firing fires EXACTLY once (never lost, never
doubled) and the single controller is never double-booked.

Pure logic — uses the real next_runs + resolve_conflicts + due_runs_since,
no HA. Mirrors ScheduleCoordinator._tick's planning core.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from itertools import combinations, pairwise

from custom_components.complete_irrigation.chunked_run import plan_blocks
from custom_components.complete_irrigation.conflict_resolver import (
    DEFAULT_CASCADE_CAP_MINUTES,
    POLICY_DEFER_NEW,
    REASON_COMMITTED,
    committed_runs_from_sessions,
    resolve_conflicts,
    stale_session_zones,
)
from custom_components.complete_irrigation.const import (
    DEFAULT_BUFFER_MINUTES,
    DEFAULT_COMPRESS_FLOOR_PCT,
)
from custom_components.complete_irrigation.gap_insertion import (
    REASON_GAP_INSERTED,
    insertion_key,
    plan_gap_insertions,
)
from custom_components.complete_irrigation.run_planner import (
    due_runs_since,
    longest_run_span_minutes,
    next_runs,
)
from custom_components.complete_irrigation.schedule import Schedule

DAY = datetime(2026, 6, 16, 0, 0, 0)  # a Tuesday
UDAY = datetime(2026, 6, 16, 0, 0, 0, tzinfo=UTC)  # aware twin (sessions need tz)


def _sched(sid, hh, mm, dur) -> Schedule:
    return Schedule(
        id=sid,
        name=sid,
        zone_entity_id=f"switch.{sid}",
        start_time=time(hh, mm),
        duration_minutes=dur,
        weekdays=(0, 1, 2, 3, 4, 5, 6),  # daily
    )


def simulate(scheds, start, end):
    """Run the coordinator's tick core minute-by-minute. Returns the list
    of fires: dicts with key, scheduled_start, fire_time, minutes."""
    fired: dict[tuple, dict] = {}  # key -> {start, minutes} (committed blockers)
    fires: list[dict] = []
    last = None
    now = start
    while now <= end:
        if last is None:
            last = now
            now += timedelta(minutes=1)
            continue

        # Lookahead covers the longest run (so a long run sees what it'll
        # collide with); lookback is generous (a full day) so a run deferred
        # behind a multi-hour backlog stays visible — both as a pending run
        # to fire and, once fired, as a committed blocker. The fired-set
        # excludes already-fired runs from being re-planned.
        horizon = timedelta(
            minutes=max(
                DEFAULT_CASCADE_CAP_MINUTES,
                longest_run_span_minutes(scheds, None) + DEFAULT_BUFFER_MINUTES,
            )
        )
        upcoming = next_runs(scheds, from_dt=last - timedelta(hours=24), until_dt=now + horizon)

        # Overlay already-fired runs as committed blockers (actual start+dur).
        overlaid = []
        for r in upcoming:
            key = (r.schedule_id, r.start_at.isoformat())
            if key in fired:
                info = fired[key]
                r = replace(
                    r,
                    start_at=info["start"],
                    duration_minutes=info["minutes"],
                    reason=REASON_COMMITTED,
                )
            overlaid.append(r)

        resolved = resolve_conflicts(
            overlaid,
            POLICY_DEFER_NEW,
            compress_floor_pct=DEFAULT_COMPRESS_FLOOR_PCT,
        )
        due = due_runs_since(resolved, last, now)
        for run in due:
            if run.reason == REASON_COMMITTED:
                continue  # already fired, just a blocker
            # key by the ORIGINAL scheduled firing (recover via scheduled_start_at)
            key = (run.schedule_id, run.scheduled_start_at.isoformat())
            if key in fired:
                continue  # defensive: never fire twice
            fired[key] = {"start": now, "minutes": run.duration_minutes}
            fires.append(
                {
                    "key": key,
                    "scheduled": run.scheduled_start_at,
                    "fire_time": now,
                    "minutes": run.duration_minutes,
                }
            )
        last = now
        now += timedelta(minutes=1)
    return fires


def _expected_firings(scheds, start, end):
    """Ground truth: every scheduled firing in [start, end], keyed."""
    runs = next_runs(scheds, from_dt=start, until_dt=end + timedelta(minutes=1))
    return {(r.schedule_id, r.start_at.isoformat()) for r in runs}


def test_no_run_is_ever_lost_under_giant_overlap():
    # A 4.5h giant straddles the morning; three small daily runs schedule
    # right into its shadow. Every one must still fire exactly once.
    scheds = [
        _sched("trees", 2, 0, 270),  # 02:00-06:30
        _sched("garden", 6, 0, 15),  # scheduled mid-giant
        _sched("grass", 6, 30, 20),  # scheduled at giant's tail
        _sched("citrus", 7, 30, 240),  # 7.5h... starts right after
    ]
    start = DAY + timedelta(hours=1)  # 01:00
    end = DAY + timedelta(hours=13)  # 13:00
    fires = simulate(scheds, start, end)

    fired_keys = {f["key"] for f in fires}
    expected = _expected_firings(scheds, start, end)

    missing = expected - fired_keys
    assert not missing, f"LOST runs (never fired): {sorted(missing)}"

    # exactly once each
    seen = [f["key"] for f in fires]
    assert len(seen) == len(set(seen)), "a run fired more than once"

    # controller never double-booked: order by fire time, each run's
    # [fire_time, fire_time+minutes) must not overlap the next fire.
    ordered = sorted(fires, key=lambda f: f["fire_time"])
    for a, b in pairwise(ordered):
        a_end = a["fire_time"] + timedelta(minutes=a["minutes"])
        assert b["fire_time"] >= a_end, (
            f"DOUBLE-BOOKED: {a['key']} runs {a['fire_time']}..{a_end}, "
            f"but {b['key']} fires {b['fire_time']}"
        )


def test_simple_daily_runs_all_fire_once():
    scheds = [_sched("a", 6, 0, 15), _sched("b", 8, 0, 20), _sched("c", 18, 0, 10)]
    start = DAY + timedelta(hours=5)
    end = DAY + timedelta(hours=19)
    fires = simulate(scheds, start, end)
    assert {f["key"] for f in fires} == _expected_firings(scheds, start, end)


# ── v1.39: session-aware simulation (chunked delivery + gap insertion) ──────
#
# The simulator above models committed blockers with a bare overlay; the one
# below mirrors the coordinator's ACTUAL _tick pipeline — active_run_sessions
# records (with block plans, as services._record_active_session writes them),
# committed_runs_from_sessions, the fired-occurrence dedupe, the gap-insertion
# ledger + plan, resolve, due — plus a physical model of the controller: every
# valve-open interval (per BLOCK for chunked runs, gaps idle) so one-zone-at-
# a-time can be asserted against real open-valve time, not whole-run spans.


def simulate_sessions(
    scheds, start, end, *, cap_min=58, gap_seconds=1200, insertion=True, stop=None
):
    """Coordinator-faithful tick loop. Returns (fires, busy):
    fires  — one dict per logical firing (key, zone, scheduled, fire_time,
             minutes, reason);
    busy   — (zone, valve_on, valve_off) controller-open intervals, one per
             block for chunked runs (the inter-block gaps are idle).
    stop   — optional (zone, at_dt): the user stops that zone's run at that
             minute (session cleared, its open interval truncated) — the
             stop-mid-insertion scenario.
    """
    sessions: dict[str, dict] = {}
    ledger: dict[str, str] = {}  # coordinator's gap_insertions_fired
    fires: list[dict] = []
    busy: list[tuple[str, datetime, datetime]] = []
    last = None
    now = start
    while now <= end:
        if last is None:
            last = now
            now += timedelta(minutes=1)
            continue

        if stop is not None and now >= stop[1] and stop[0] in sessions:
            sessions.pop(stop[0], None)  # what handle_stop_zone does
            busy = [(z, on, min(off, now)) if z == stop[0] else (z, on, off) for z, on, off in busy]

        # coordinator._prune_stale_sessions
        for z in stale_session_zones(sessions, now):
            sessions.pop(z, None)

        horizon = timedelta(
            minutes=max(
                DEFAULT_CASCADE_CAP_MINUTES,
                longest_run_span_minutes(scheds, None) + DEFAULT_BUFFER_MINUTES,
            )
        )
        upcoming = next_runs(scheds, from_dt=last - timedelta(hours=24), until_dt=now + horizon)

        committed = committed_runs_from_sessions(sessions, now)
        if committed:
            fired_pairs = {(c.schedule_id, c.zone_entity_id) for c in committed if c.schedule_id}
            upcoming = [
                r
                for r in upcoming
                if (r.schedule_id, r.zone_entity_id) not in fired_pairs or r.start_at > now
            ]
        if ledger:
            upcoming = [r for r in upcoming if insertion_key(r) not in ledger]
        if insertion:
            plan = plan_gap_insertions(upcoming, sessions, now, fired_keys=frozenset(ledger))
            if plan:
                upcoming = [
                    (
                        replace(r, start_at=pin, reason=REASON_GAP_INSERTED)
                        if (pin := plan.get(insertion_key(r))) is not None
                        else r
                    )
                    for r in upcoming
                ]

        resolved = resolve_conflicts(committed + upcoming, POLICY_DEFER_NEW, compress_floor_pct=100)
        for run in due_runs_since(resolved, last, now):
            if run.reason == REASON_COMMITTED:
                continue
            if run.reason == REASON_GAP_INSERTED:
                ledger[insertion_key(run)] = now.isoformat()
            _fire_sim(run, now, sessions, busy, fires, cap_min, gap_seconds)
        last = now
        now += timedelta(minutes=1)
    return fires, busy


def _fire_sim(run, now, sessions, busy, fires, cap_min, gap_seconds):
    """Model run_zone: record the session exactly as _record_active_session
    does (block plan included for chunked runs) and open the valve — per
    block, with the timer offsets _dispatch_run_blocks arms."""
    zone = run.zone_entity_id
    minutes = int(run.duration_minutes)
    blocks = plan_blocks(minutes, cap_min)
    session = {
        "total_minutes": minutes,
        "started_at": now.isoformat(),
        "deadline": (
            now + timedelta(minutes=minutes, seconds=(len(blocks) - 1) * gap_seconds)
        ).isoformat(),
        "water_deadline": (now + timedelta(minutes=minutes)).isoformat(),
        "schedule_id": run.schedule_id,
        "schedule_name": run.schedule_name,
        "source": "scheduled",
    }
    if len(blocks) > 1:
        session["blocks"] = blocks
        session["block_gap_seconds"] = gap_seconds
        cursor = now
        for b in blocks:
            busy.append((zone, cursor, cursor + timedelta(minutes=b)))
            cursor += timedelta(minutes=b, seconds=gap_seconds)
    else:
        busy.append((zone, now, now + timedelta(minutes=minutes)))
    sessions[zone] = session
    fires.append(
        {
            "key": (run.schedule_id, run.scheduled_start_at.isoformat()),
            "zone": zone,
            "scheduled": run.scheduled_start_at,
            "fire_time": now,
            "minutes": minutes,
            "reason": run.reason,
        }
    )


def _assert_never_two_valves_open(busy):
    """The inviolable invariant: no two valve-open intervals overlap — not
    across zones (one-zone-at-a-time) and not within one (no self re-fire)."""
    for (za, a1, a2), (zb, b1, b2) in combinations(sorted(busy, key=lambda x: x[1]), 2):
        assert a2 <= b1 or b2 <= a1, f"TWO VALVES OPEN: {za} {a1}..{a2} vs {zb} {b1}..{b2}"


def _gap_windows_of(start, blocks, gap_seconds):
    """(gap_start, gap_end) windows for a chunked run fired at `start`."""
    out = []
    cursor = start
    for b in blocks[:-1]:
        block_end = cursor + timedelta(minutes=b)
        out.append((block_end, block_end + timedelta(seconds=gap_seconds)))
        cursor = block_end + timedelta(seconds=gap_seconds)
    return out


# 280 min at cap 58 -> [58, 58, 58, 58, 48]: the 5-block giant of the scenario.
CHUNK_SCHEDS = [
    _sched("citrus", 6, 0, 280),
    _sched("garden", 6, 30, 15),  # short zone on the shared manifold, mid-giant
]
CITRUS_BLOCKS = [58, 58, 58, 58, 48]


def test_short_run_fires_inside_a_gap_of_a_five_block_run():
    """The headline scenario: a 15-min run scheduled inside a 5-block giant's
    window fires in an inter-block gap (20-min gaps), both complete, and the
    controller never has two valves open — minute-by-minute."""
    start, end = UDAY + timedelta(hours=5), UDAY + timedelta(hours=14)
    fires, busy = simulate_sessions(CHUNK_SCHEDS, start, end, gap_seconds=1200)

    assert {f["key"] for f in fires} == _expected_firings(CHUNK_SCHEDS, start, end)
    assert len(fires) == len({f["key"] for f in fires})  # exactly once each

    citrus_start = next(f["fire_time"] for f in fires if f["zone"] == "switch.citrus")
    assert citrus_start == UDAY + timedelta(hours=6)  # fired on time

    garden = next(f for f in fires if f["zone"] == "switch.garden")
    assert garden["reason"] == REASON_GAP_INSERTED
    gap1, *_ = _gap_windows_of(citrus_start, CITRUS_BLOCKS, 1200)
    # Fired ENTIRELY inside gap 1 (06:58-07:18): pin 06:59:30 -> 07:00 tick.
    assert garden["fire_time"] == UDAY + timedelta(hours=7)
    assert gap1[0] <= garden["fire_time"]
    assert garden["fire_time"] + timedelta(minutes=15) <= gap1[1]

    # The giant's cadence never shifted: its valve-open intervals are EXACTLY
    # the 5 planned block windows.
    citrus_busy = sorted((on, off) for z, on, off in busy if z == "switch.citrus")
    cursor, expected_blocks = citrus_start, []
    for b in CITRUS_BLOCKS:
        expected_blocks.append((cursor, cursor + timedelta(minutes=b)))
        cursor += timedelta(minutes=b, seconds=1200)
    assert citrus_busy == expected_blocks

    _assert_never_two_valves_open(busy)


def test_without_insertion_the_same_short_run_defers_hours():
    """Control: insertion disabled -> the 15-min run waits for the whole
    gap-inclusive span (~6h) and fires after it — proving insertion is what
    changes the outcome (and that the fallback path still never loses it)."""
    start, end = UDAY + timedelta(hours=5), UDAY + timedelta(hours=14)
    fires, busy = simulate_sessions(CHUNK_SCHEDS, start, end, gap_seconds=1200, insertion=False)

    assert {f["key"] for f in fires} == _expected_firings(CHUNK_SCHEDS, start, end)
    garden = next(f for f in fires if f["zone"] == "switch.garden")
    citrus_wall_end = UDAY + timedelta(hours=6, minutes=280, seconds=4 * 1200)  # 12:00
    assert garden["fire_time"] >= citrus_wall_end
    _assert_never_two_valves_open(busy)


def test_exactly_fitting_gap_hosts_the_run():
    # required = 15*60 + 2*(30+60) = 1080s. A 1080s gap fits exactly.
    start, end = UDAY + timedelta(hours=5), UDAY + timedelta(hours=14)
    fires, busy = simulate_sessions(CHUNK_SCHEDS, start, end, gap_seconds=1080)
    garden = next(f for f in fires if f["zone"] == "switch.garden")
    assert garden["reason"] == REASON_GAP_INSERTED
    gap1, *_ = _gap_windows_of(UDAY + timedelta(hours=6), CITRUS_BLOCKS, 1080)
    assert gap1[0] <= garden["fire_time"]
    assert garden["fire_time"] + timedelta(minutes=15) <= gap1[1]
    _assert_never_two_valves_open(busy)


def test_one_second_too_small_gap_defers_exactly_as_today():
    # 1079s < required 1080s -> NO insertion; the run defers behind the whole
    # span and still fires exactly once (zero regression).
    start, end = UDAY + timedelta(hours=5), UDAY + timedelta(hours=14)
    fires, busy = simulate_sessions(CHUNK_SCHEDS, start, end, gap_seconds=1079)
    assert {f["key"] for f in fires} == _expected_firings(CHUNK_SCHEDS, start, end)
    garden = next(f for f in fires if f["zone"] == "switch.garden")
    assert garden["reason"] != REASON_GAP_INSERTED
    citrus_last_off = max(off for z, _, off in busy if z == "switch.citrus")
    assert garden["fire_time"] >= citrus_last_off
    _assert_never_two_valves_open(busy)


def test_stopped_inserted_run_leaves_remaining_blocks_intact():
    """Constraint: stopping the inserted short run mid-gap must not disturb
    the giant's remaining blocks — and the stopped run is never re-fired."""
    start, end = UDAY + timedelta(hours=5), UDAY + timedelta(hours=14)
    stop_at = UDAY + timedelta(hours=7, minutes=5)  # 5 min into the inserted run
    fires, busy = simulate_sessions(
        CHUNK_SCHEDS, start, end, gap_seconds=1200, stop=("switch.garden", stop_at)
    )

    garden_fires = [f for f in fires if f["zone"] == "switch.garden"]
    assert len(garden_fires) == 1  # fired once, never re-inserted/re-fired
    assert garden_fires[0]["fire_time"] == UDAY + timedelta(hours=7)
    garden_busy = [(on, off) for z, on, off in busy if z == "switch.garden"]
    assert garden_busy == [(UDAY + timedelta(hours=7), stop_at)]  # truncated by the stop

    # Every citrus block delivered exactly as planned — cadence untouched.
    citrus_busy = sorted((on, off) for z, on, off in busy if z == "switch.citrus")
    cursor, expected_blocks = UDAY + timedelta(hours=6), []
    for b in CITRUS_BLOCKS:
        expected_blocks.append((cursor, cursor + timedelta(minutes=b)))
        cursor += timedelta(minutes=b, seconds=1200)
    assert citrus_busy == expected_blocks
    _assert_never_two_valves_open(busy)


def test_two_shorts_take_two_different_gaps():
    """Two short runs blocked by the same giant each get their own gap —
    never the same one — and everything still fires exactly once."""
    scheds = [
        _sched("citrus", 6, 0, 280),
        _sched("garden", 6, 30, 15),
        _sched("grass", 6, 40, 10),
    ]
    start, end = UDAY + timedelta(hours=5), UDAY + timedelta(hours=14)
    fires, busy = simulate_sessions(scheds, start, end, gap_seconds=1200)

    assert {f["key"] for f in fires} == _expected_firings(scheds, start, end)
    gaps = _gap_windows_of(UDAY + timedelta(hours=6), CITRUS_BLOCKS, 1200)
    garden = next(f for f in fires if f["zone"] == "switch.garden")
    grass = next(f for f in fires if f["zone"] == "switch.grass")
    assert garden["reason"] == grass["reason"] == REASON_GAP_INSERTED
    # Earliest-scheduled short takes gap 1; the other is serialized into gap 2.
    assert gaps[0][0] <= garden["fire_time"] <= gaps[0][1] - timedelta(minutes=15)
    assert gaps[1][0] <= grass["fire_time"] <= gaps[1][1] - timedelta(minutes=10)
    _assert_never_two_valves_open(busy)


def test_randomized_chunked_days_fire_exactly_once_and_never_overlap():
    """Fuzz: one chunked giant + one random short, random gap sizes (including
    the 30s default where nothing ever fits and everything defers as today).
    Invariants: every firing fires exactly once; no two valve-open intervals
    ever overlap.

    Deliberately ONE short zone: with 2+ shorts chain-deferred behind a
    chunked giant, HEAD (v1.38.1) already LOSES the second short whenever the
    total inter-block gap time exceeds the blocker catch margin — the pruned
    session's past 'ghost' occurrence carries its WATER duration, shorter
    than the gap-inclusive wall span, so the chained run's placement snaps
    into the past and strands. Pre-existing bug, reproduced without any gap
    insertion, tracked separately; widen this fuzz when it is fixed."""
    import random

    for seed in range(40):
        rng = random.Random(seed)
        gap_s = rng.choice([30, 300, 600, 1080, 1200])
        scheds = [
            _sched("citrus", 6, 0, rng.choice([120, 200, 280, 300])),
            _sched(
                "z0",
                rng.randint(6, 13),
                rng.choice([0, 10, 20, 30, 40, 50]),
                rng.choice([5, 8, 10, 15, 20]),
            ),
        ]
        start, end = UDAY + timedelta(hours=5), UDAY + timedelta(hours=22)
        fires, busy = simulate_sessions(scheds, start, end, gap_seconds=gap_s)
        shapes = [(s.id, str(s.start_time), s.duration_minutes) for s in scheds]
        ctx = f"seed={seed} gap={gap_s} scheds={shapes}"

        fired = [f["key"] for f in fires]
        assert set(fired) == _expected_firings(scheds, start, end), f"lost/extra: {ctx}"
        assert len(fired) == len(set(fired)), f"double fire: {ctx}"
        _assert_never_two_valves_open(busy)


def test_randomized_days_never_lose_a_run_or_double_book():
    """Fuzz: random feasible daily schedules through the full tick loop.
    Load is kept well under the day so every run has room — any lost run or
    double-booking is a real serialization bug, not just an out-of-time defer."""
    import random

    for seed in range(80):
        rng = random.Random(seed)
        n = rng.randint(2, 6)
        scheds = []
        for i in range(n):
            hh = rng.randint(2, 18)
            mm = rng.choice([0, 10, 15, 30, 45])
            dur = rng.choice([5, 10, 15, 20, 40, 90])
            scheds.append(_sched(f"z{i}", hh, mm, dur))
        start = DAY
        end = DAY + timedelta(hours=23, minutes=59)
        fires = simulate(scheds, start, end)
        ctx = (
            f"seed={seed} scheds={[(s.id, s.start_time.hour, s.duration_minutes) for s in scheds]}"
        )

        fired = {f["key"] for f in fires}
        expected = _expected_firings(scheds, start, end)
        assert fired == expected, f"lost/extra runs: {expected ^ fired} | {ctx}"

        ordered = sorted(fires, key=lambda f: f["fire_time"])
        for a, b in pairwise(ordered):
            a_end = a["fire_time"] + timedelta(minutes=a["minutes"])
            assert b["fire_time"] >= a_end, f"double-booked: {a['key']} vs {b['key']} | {ctx}"
