"""v1.25 — chunked-run block cancellation.

Regression guard for the security-gate finding: blocks 2..n were scheduled with
async_call_later but their cancel handles were never stored, so Stop / delete /
reload could not cancel pending blocks and the valve re-opened for each remaining
block (runaway re-watering after the user pressed Stop).

These tests exercise the two pure-ish helpers in services.py with a fake hass, so
they need HA importable (services imports homeassistant.* at module load) — they
run in CI and skip on a bare dev machine, like test_services_schemas.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("homeassistant.const")

from custom_components.complete_irrigation import services

ZONE = "switch.front_back_trees"


def _make_hass():
    """Fake hass that records run_zone dispatches without creating coroutines
    (filterwarnings=error would turn a never-awaited coroutine into a failure)."""
    hass = MagicMock()
    calls: list[dict] = []

    def _async_call(domain, service, data, blocking=False):
        calls.append({"service": service, "data": data})
        return MagicMock()  # sync — not a coroutine

    hass.services.async_call.side_effect = _async_call
    hass.async_create_task = MagicMock()
    return hass, calls


def _patch_timers(monkeypatch):
    """Capture scheduled (delay, callback) pairs and hand back cancel mocks."""
    captured: list[tuple] = []
    cancels: list[MagicMock] = []

    def _fake_acl(_hass, delay, cb):
        captured.append((delay, cb))
        c = MagicMock()
        cancels.append(c)
        return c

    monkeypatch.setattr(services, "async_call_later", _fake_acl)
    return captured, cancels


def test_dispatch_fires_block1_and_stores_later_timers(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, calls = _make_hass()
    entry_data: dict = {}

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58, 10], 30, None)

    # Block 1 fires immediately; blocks 2 & 3 are scheduled (stored).
    assert len(calls) == 1
    assert calls[0]["data"] == {"entity_id": ZONE, "minutes": 58}
    assert len(captured) == 2
    assert len(entry_data["block_timers"][ZONE]) == 2
    assert entry_data["block_seq"][ZONE] == 1


def test_stop_cancels_pending_blocks_and_neuters_inflight(monkeypatch):
    captured, cancels = _patch_timers(monkeypatch)
    hass, calls = _make_hass()
    entry_data: dict = {}

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58, 10], 30, None)
    assert len(calls) == 1  # block 1 only, so far

    # Simulate Stop: cancel block timers (what handle_stop_zone now calls).
    services._cancel_block_timers(entry_data, ZONE)
    assert all(c.called for c in cancels)  # every pending timer cancelled
    assert entry_data["block_seq"][ZONE] != 1  # generation advanced past the run
    assert ZONE not in entry_data.get("block_timers", {})  # pending timers cleared

    # A block timer that already slipped onto the loop must be a no-op now.
    _delay, block2_cb = captured[0]
    block2_cb()
    assert len(calls) == 1  # unchanged — the generation guard skipped it


def test_without_cancel_a_pending_block_would_fire(monkeypatch):
    """Positive control: absent a Stop, an elapsed block timer DOES fire."""
    captured, _cancels = _patch_timers(monkeypatch)
    hass, calls = _make_hass()
    entry_data: dict = {}

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58, 10], 30, None)
    _delay, block2_cb = captured[0]
    block2_cb()
    assert len(calls) == 2  # block 2 fired its run_zone
    assert calls[1]["data"] == {"entity_id": ZONE, "minutes": 58}


def test_new_sequence_supersedes_old(monkeypatch):
    _captured, cancels = _patch_timers(monkeypatch)
    hass, _calls = _make_hass()
    entry_data: dict = {}

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58], 30, None)
    first_cancels = list(cancels)
    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58], 30, None)

    # The first sequence's timer was cancelled when the second started.
    assert all(c.called for c in first_cancels)
    assert entry_data["block_seq"][ZONE] == 2  # generation advanced


def test_stopping_another_zone_leaves_pending_blocks_intact(monkeypatch):
    """v1.39 gap insertion — a short run inserted into the long run's
    inter-block gap may be Stopped mid-run. That stop cancels timers for the
    SHORT zone only; the long run's queued blocks must stay armed and fire."""
    captured, cancels = _patch_timers(monkeypatch)
    hass, calls = _make_hass()
    entry_data: dict = {}
    short_zone = "switch.garden"

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58, 10], 1200, None)
    long_cancels = list(cancels)

    # The user stops the inserted short run (what handle_stop_zone calls).
    services._cancel_block_timers(entry_data, short_zone)

    assert not any(c.called for c in long_cancels)  # long run's timers untouched
    assert len(entry_data["block_timers"][ZONE]) == 2  # blocks 2+3 still armed
    assert entry_data["block_seq"][ZONE] == 1  # generation NOT advanced

    # And the queued blocks still fire when their timers elapse.
    for _delay, cb in captured:
        cb()
    assert [c["data"]["minutes"] for c in calls] == [58, 58, 10]
