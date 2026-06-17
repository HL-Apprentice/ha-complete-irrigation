"""Tests for run_guard.external_off_decision (pure logic, v1.21).

Decides what to do when a zone reports off mid-run after the debounce:
recover (it's back on), retry (re-assert the switch), or abort.
"""

from __future__ import annotations

from custom_components.complete_irrigation.run_guard import (
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
