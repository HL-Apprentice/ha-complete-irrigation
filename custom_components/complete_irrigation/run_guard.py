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


def external_off_decision(state_now: str, reasserts_left: int, before_deadline: bool) -> str:
    """Decide what to do when a zone reports off mid-run, AFTER the debounce.

    • "recovered" — the zone is back on; the off was a transient flap.
    • "retry"     — still off, but within the run window and we have
                    re-assert attempts left → turn it back on.
    • "abort"     — still off and out of attempts (or past the deadline)
                    → record the abort and alert.
    """
    if state_now == "on":
        return "recovered"
    if before_deadline and reasserts_left > 0:
        return "retry"
    return "abort"
