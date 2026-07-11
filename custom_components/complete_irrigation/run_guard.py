"""Pure-logic helpers guarding an in-flight zone run (v1.21).

Kept HA-free so the decision can be unit-tested without a running Home
Assistant. The wiring (timers, state listeners, service calls) lives in
services.py; this module only decides *what* to do.
"""

from __future__ import annotations

# Cloud irrigation integrations (e.g. Rachio) momentarily report a zone
# "off" for a few seconds right after we turn it on — a stale cloud poll
# racing our optimistic state update. The zone is physically running; the
# "off" is a lie. So a mid-run "off" is debounced this long before we react.
EXTERNAL_OFF_DEBOUNCE_SECONDS = 90
# After the debounce, if the zone is *still* off we re-assert the switch on
# this many times (recovering a genuinely-dropped run) before giving up.
EXTERNAL_OFF_MAX_REASSERTS = 2
# v1.22 — a zone that ran at least this long before stopping is treated as a
# legitimate controller cap-stop, not a flap/fault: Rachio stops a zone at its
# per-zone "manual run minutes" cap (e.g. 60), so a long CI run (Trees 270)
# arrives here every cap-length. Re-asserting AND resetting the budget on a
# healthy chunk lets such a run ride cap after cap to its full CI duration,
# while a zone that can't stay on past this threshold still drains the budget
# and aborts quickly. Comfortably above a few-second flap, below any sane cap.
EXTERNAL_OFF_HEALTHY_RUN_SECONDS = 120


def external_off_decision(
    state_now: str,
    reasserts_left: int,
    before_deadline: bool,
    ran_healthy: bool = False,
) -> str:
    """Decide what to do when a zone reports off mid-run, AFTER the debounce.

    • "recovered"   — the zone is back on; the off was a transient flap.
    • "retry_reset" — still off, within the run window, and it had been
                      running healthily (a controller cap-stop) → re-assert
                      AND refill the re-assert budget so a long run rides
                      cap after cap to its full duration.
    • "retry"       — still off, within the window, no healthy chunk but we
                      still have budget left → re-assert (flap/fault).
    • "abort"       — still off and out of budget (or past the deadline)
                      → record the abort and alert.
    """
    if state_now == "on":
        return "recovered"
    if not before_deadline:
        return "abort"
    if ran_healthy:
        return "retry_reset"
    if reasserts_left > 0:
        return "retry"
    return "abort"


# v1.40 — "check back" valve-actuation verification (inspired by Irrigation
# Unlimited's check_back). After commanding the zone switch on/off we re-read
# its state this many seconds later and react if the command didn't take
# (Rachio-cloud/WiFi command loss: the service call "succeeds" but the valve
# never actuates and the switch state never flips). 0 disables the check.
DEFAULT_VERIFY_SWITCH_SECONDS = 30


def valve_verify_on_decision(
    run_live: bool,
    state_now: str,
    external_off_pending: bool,
    first_attempt: bool,
) -> str:
    """Decide what to do when the post-turn-ON verify check fires.

    • "ended"          — the run is no longer live (auto-stop / Stop / abort
                         already tore it down); nothing left to verify.
    • "verified"       — the switch reports on; the command actuated.
    • "defer_external" — the switch is off but the off was DETECTED by the
                         run's state listener (its debounce is pending): the
                         switch DID come on and something — possibly the
                         user — turned it off. The external-off machinery
                         owns that path (re-assert budget / abort); retrying
                         on from here would fight it and override the user.
    • "retry"          — off with no detected off-transition (the turn-on was
                         most likely lost in flight): re-send turn_on once.
    • "fail"           — still off after the retry: the valve never actuated.
    """
    if not run_live:
        return "ended"
    if state_now == "on":
        return "verified"
    if external_off_pending:
        return "defer_external"
    return "retry" if first_attempt else "fail"


def valve_verify_off_decision(
    new_run_live: bool,
    state_now: str,
    first_attempt: bool,
) -> str:
    """Decide what to do when the post-turn-OFF verify check fires.

    • "superseded" — a NEW run started on this zone inside the verify window
                     (a fresh manual run, or the next chunked block): it owns
                     the valve now; enforcing the old off would kill it.
    • "verified"   — the switch no longer reports on; the valve closed.
    • "retry"      — still on: re-send turn_off once.
    • "fail"       — still on after the retry: the valve may be stuck open.
    """
    if new_run_live:
        return "superseded"
    if state_now != "on":
        return "verified"
    return "retry" if first_attempt else "fail"
