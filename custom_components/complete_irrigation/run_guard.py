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
