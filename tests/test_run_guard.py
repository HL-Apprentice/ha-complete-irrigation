"""Tests for run_guard.external_off_decision (pure logic, v1.21-v1.22).

Decides what to do when a zone reports off mid-run after the debounce:
recover (it's back on), retry / retry_reset (re-assert the switch), or abort.
"""

from __future__ import annotations

from custom_components.complete_irrigation.run_guard import (
    EXTERNAL_OFF_HEALTHY_RUN_SECONDS,
    EXTERNAL_OFF_MAX_REASSERTS,
    external_off_decision,
)


def test_back_on_recovers():
    # A transient flap that corrected itself — never touch the run.
    assert external_off_decision("on", reasserts_left=2, before_deadline=True) == "recovered"
    # Even with no attempts left / past deadline, "on" always recovers.
    assert external_off_decision("on", reasserts_left=0, before_deadline=False) == "recovered"


def test_still_off_with_attempts_retries():
    assert external_off_decision("off", reasserts_left=2, before_deadline=True) == "retry"
    assert external_off_decision("off", reasserts_left=1, before_deadline=True) == "retry"
    # Unavailable is treated like off (not "on") → still recoverable via retry.
    assert external_off_decision("unavailable", reasserts_left=1, before_deadline=True) == "retry"


def test_out_of_attempts_aborts():
    assert external_off_decision("off", reasserts_left=0, before_deadline=True) == "abort"


def test_past_deadline_aborts_even_with_attempts():
    # No point re-asserting a run whose time is already up.
    assert external_off_decision("off", reasserts_left=2, before_deadline=False) == "abort"


def test_reassert_budget_is_positive():
    assert EXTERNAL_OFF_MAX_REASSERTS >= 1


# ── v1.22 — healthy cap-stop refills the budget ─────────────────────


def test_healthy_chunk_retries_and_resets_even_with_no_budget():
    # A long run that Rachio stopped at its per-zone cap (it ran a healthy
    # chunk first) must keep going — re-assert AND refill the budget, even
    # when reasserts_left has hit 0. This is what carries Trees/Citrus to
    # their full CI duration across repeated cap-stops.
    assert (
        external_off_decision("off", reasserts_left=0, before_deadline=True, ran_healthy=True)
        == "retry_reset"
    )
    assert (
        external_off_decision("off", reasserts_left=2, before_deadline=True, ran_healthy=True)
        == "retry_reset"
    )


def test_healthy_chunk_past_deadline_still_aborts():
    # Even a healthy run that's reached its deadline stops — no overrun.
    assert (
        external_off_decision("off", reasserts_left=2, before_deadline=False, ran_healthy=True)
        == "abort"
    )


def test_unhealthy_flap_falls_back_to_limited_budget():
    # A zone that can't stay on past the healthy threshold (flap/fault)
    # spends the bounded budget and then aborts — no infinite hammering.
    assert (
        external_off_decision("off", reasserts_left=1, before_deadline=True, ran_healthy=False)
        == "retry"
    )
    assert (
        external_off_decision("off", reasserts_left=0, before_deadline=True, ran_healthy=False)
        == "abort"
    )


def test_healthy_threshold_is_sane():
    # Above a few-second cloud flap, below any reasonable controller cap.
    assert 30 <= EXTERNAL_OFF_HEALTHY_RUN_SECONDS <= 600
