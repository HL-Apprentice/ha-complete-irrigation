"""v1.30 restart fail-over — coordinator glue (_resume_interrupted_runs).

The resume DECISION is unit-tested purely in test_session_resume. THESE cover the
coordinator's HA wiring: a resumable session re-fires run_zone for the remaining
minutes (stashing resumed meta); a past-its-end session turns the valve off +
closes the record; no sessions = no-op. Needs HA importable, like the other
coordinator-tier tests.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant.util.dt")

from homeassistant.util import dt as dt_util

from custom_components.complete_irrigation.const import DOMAIN
from custom_components.complete_irrigation.coordinator import ScheduleCoordinator


def _coord_with_sessions(sessions) -> ScheduleCoordinator:
    coord = ScheduleCoordinator(MagicMock(), "e1")
    coord._hass.services.async_call = AsyncMock()
    coord._hass.data = {DOMAIN: {"e1": {}}}
    coord.async_save_config = AsyncMock()
    coord.async_save_run_history = AsyncMock()
    coord._config["active_run_sessions"] = sessions
    return coord


async def test_resume_fires_run_zone_for_remaining_minutes():
    coord = _coord_with_sessions(
        {
            "switch.citrus": {
                "deadline": (dt_util.now() + timedelta(minutes=40)).isoformat(),
                "total_minutes": 240,
                "schedule_id": "s1",
                "schedule_name": "Citrus",
            }
        }
    )
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    run_calls = [c for c in calls if c.args[1] == "run_zone"]
    assert len(run_calls) == 1
    assert run_calls[0].args[2]["entity_id"] == "switch.citrus"
    assert run_calls[0].args[2]["minutes"] == 40
    # Resumed meta stashed so it records under the schedule, marked resumed.
    meta = coord._hass.data[DOMAIN]["e1"]["pending_run_meta"]["switch.citrus"]
    assert "(resumed)" in meta["schedule_name"]
    assert meta["requested_minutes"] == 40


async def test_close_turns_valve_off_and_drops_session():
    coord = _coord_with_sessions(
        {"switch.bb": {"deadline": (dt_util.now() - timedelta(minutes=10)).isoformat()}}
    )
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    off = [c for c in calls if c.args[0] == "switch" and c.args[1] == "turn_off"]
    assert off and off[0].args[2]["entity_id"] == "switch.bb"
    # No run_zone for a past-its-end run; session dropped.
    assert not any(c.args[1] == "run_zone" for c in calls)
    assert "switch.bb" not in coord._config.get("active_run_sessions", {})


async def test_bad_session_fails_safe_to_close():
    coord = _coord_with_sessions({"switch.z": {"deadline": "garbage"}})
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    assert any(c.args[1] == "turn_off" for c in calls)
    assert not any(c.args[1] == "run_zone" for c in calls)


async def test_noop_when_no_sessions():
    coord = _coord_with_sessions({})
    await coord._resume_interrupted_runs()
    coord._hass.services.async_call.assert_not_called()


async def test_resume_defers_and_retries_when_switch_unavailable(monkeypatch):
    # The re-gate fix: a slow switch reconnect at resume time must NOT lose the
    # run — keep the session and retry, never fire run_zone into an unavailable
    # switch (which would refuse) and never drop the session.
    coord = _coord_with_sessions(
        {"switch.citrus": {"deadline": (dt_util.now() + timedelta(minutes=40)).isoformat()}}
    )
    unavailable = MagicMock()
    unavailable.state = "unavailable"
    coord._hass.states.get.return_value = unavailable
    scheduled = []
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_call_later",
        lambda *a, **k: scheduled.append(a) or MagicMock(),
    )
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    assert not any(c.args[1] == "run_zone" for c in calls)  # didn't fire into a dead switch
    assert "switch.citrus" in coord._config["active_run_sessions"]  # session KEPT
    assert scheduled and coord._resume_attempts == 1  # a bounded retry was armed


async def test_retry_is_scoped_to_deferred_zones():
    # Multi-model fix: a retry must only re-process the deferred zones, never
    # re-fire a zone already resumed on the first pass.
    coord = _coord_with_sessions(
        {"switch.z": {"deadline": (dt_util.now() + timedelta(minutes=40)).isoformat()}}
    )
    await coord._resume_interrupted_runs(zones={"switch.other"})  # z is out of scope
    coord._hass.services.async_call.assert_not_called()


async def test_gives_up_cleanly_after_max_retries(monkeypatch):
    # Multi-model fix: out of retries must NOT leave a zombie session (false
    # "Running") — clear it, force the valve off, record an abort.
    from custom_components.complete_irrigation.coordinator import _RESUME_MAX_ATTEMPTS

    coord = _coord_with_sessions(
        {"switch.z": {"deadline": (dt_util.now() + timedelta(minutes=40)).isoformat()}}
    )
    unavailable = MagicMock()
    unavailable.state = "unavailable"
    coord._hass.states.get.return_value = unavailable
    coord._resume_attempts = _RESUME_MAX_ATTEMPTS  # already at the cap
    coord._run_history.abort_run = MagicMock()
    monkeypatch.setattr("homeassistant.helpers.event.async_call_later", lambda *a, **k: MagicMock())
    await coord._resume_interrupted_runs()
    assert "switch.z" not in coord._config.get("active_run_sessions", {})  # no zombie
    calls = coord._hass.services.async_call.call_args_list
    assert any(c.args[0] == "switch" and c.args[1] == "turn_off" for c in calls)  # valve off
    coord._run_history.abort_run.assert_called_once()
