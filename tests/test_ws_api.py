"""Websocket command tests (v1.63) — ws_api.py was at 0% coverage.

Every one of these 11 commands feeds the panel, and none had ever been executed
by a test. That is the layer where a decorator mistake shipped in v1.60.0: a
helper was accidentally wrapped by @require_admin, which made yard_report raise
for everyone until it was caught by hand on real hardware.

The commands are decorated (require_admin / websocket_command / async_response)
but the decorators set __wrapped__, so the real coroutine is reachable and can
be driven with a fake hass + connection. No Home Assistant server required.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

pytest.importorskip("homeassistant.components.websocket_api")
pytest.importorskip("voluptuous")

from custom_components.complete_irrigation import ws_api

# ── harness ─────────────────────────────────────────────────────────


def unwrap(fn):
    """Peel the HA websocket decorators to reach the handler itself."""
    inner = fn
    for _ in range(6):
        if not hasattr(inner, "__wrapped__"):
            break
        inner = inner.__wrapped__
    assert inspect.iscoroutinefunction(inner), f"{fn} did not unwrap to a coroutine"
    return inner


class FakeConnection:
    """Captures what a command sends back."""

    def __init__(self):
        self.results = []
        self.errors = []

    def send_result(self, msg_id, payload=None):
        self.results.append((msg_id, payload))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))

    @property
    def payload(self):
        assert self.results, f"no result sent (errors: {self.errors})"
        return self.results[-1][1]


class FakeHass:
    """Just enough hass for _find_coordinator to miss or hit."""

    def __init__(self, coord=None):
        self.data = {}
        if coord is not None:
            from custom_components.complete_irrigation.const import DOMAIN

            # hass.data[DOMAIN][entry_id] is a DICT holding the coordinator,
            # not the coordinator itself (see __init__._find_coordinator).
            self.data[DOMAIN] = {"entry-1": {"coordinator": coord}}


def run(coro):
    return asyncio.run(coro)


ALL_COMMANDS = [
    "list_schedules",
    "get_config",
    "get_active_runs",
    "list_run_history",
    "list_plants",
    "list_care_tasks",
    "watering_diagnosis",
    "yard_report",
    "get_daily_plan",
    "schedule_chat",
]


# ── registration + gating (the v1.60.0 class of bug) ─────────────────


def test_every_command_is_admin_gated():
    """A read command losing its admin gate is a silent privilege regression."""
    import re

    with open(ws_api.__file__) as fh:
        src = fh.read().split("\n")
    ungated = []
    for i, line in enumerate(src):
        if "@websocket_api.websocket_command" in line and not any(
            "require_admin" in src[j] for j in range(max(0, i - 4), i)
        ):
            name = "?"
            for j in range(i, min(len(src), i + 10)):
                m = re.match(r"\s*async def (\w+)", src[j])
                if m:
                    name = m.group(1)
                    break
            ungated.append(name)
    assert ungated == [], f"websocket commands missing @require_admin: {ungated}"


def test_no_module_level_helper_is_accidentally_decorated():
    """v1.60.0 shipped broken because a plain helper sat between @require_admin
    and the command it belonged to, so the decorator wrapped the HELPER. A
    helper must take its own arguments, not (hass, connection, msg)."""
    helper = ws_api._serialize_zone_regions
    params = list(inspect.signature(helper).parameters)
    assert params == ["coord"], f"_serialize_zone_regions got decorated: {params}"


def test_all_commands_unwrap_to_coroutines():
    for name in ALL_COMMANDS:
        unwrap(getattr(ws_api, name))


# ── no coordinator: every command must degrade, never raise ──────────


@pytest.mark.parametrize("name", ALL_COMMANDS)
def test_command_returns_a_safe_default_with_no_coordinator(name):
    """A command running before setup (or after an entry unloads) must send a
    usable empty result rather than raising into the websocket."""
    fn = unwrap(getattr(ws_api, name))
    conn = FakeConnection()
    msg = {"id": 1, "message": "hi", "zone_entity_id": "switch.a"}
    run(fn(FakeHass(), conn, msg))
    assert conn.results, f"{name} sent nothing with no coordinator"
    assert conn.results[-1][0] == 1


# ── get_config: the secret boundary ──────────────────────────────────


class _Coord:
    def __init__(self, config=None):
        self.config = config if config is not None else {}
        self.lockout_until = None


def test_get_config_never_ships_a_secret_to_the_browser():
    coord = _Coord(
        {
            "llm_external_api_key": "sk-must-not-leak",
            "plantnet_api_key": "pn-must-not-leak",
            "mqtt_password": "future-secret",
            "zones": {"switch.a": {"min": 20}},
        }
    )
    conn = FakeConnection()
    run(unwrap(ws_api.get_config)(FakeHass(coord), conn, {"id": 7}))
    payload = conn.payload
    blob = repr(payload)
    for secret in ("sk-must-not-leak", "pn-must-not-leak", "future-secret"):
        assert secret not in blob, f"{secret} reached the browser payload"
    assert payload["llm_external_api_key_set"] is True
    assert payload["mqtt_password_set"] is True  # fails CLOSED for new fields
    assert payload["zones"] == {"switch.a": {"min": 20}}


def test_get_config_does_not_mutate_the_live_config():
    coord = _Coord({"llm_external_api_key": "sk-keep", "zones": {}})
    run(unwrap(ws_api.get_config)(FakeHass(coord), FakeConnection(), {"id": 1}))
    assert coord.config["llm_external_api_key"] == "sk-keep", "server lost its own key"


def test_get_config_includes_lockout_state_for_the_banner():
    coord = _Coord({"zones": {}})
    conn = FakeConnection()
    run(unwrap(ws_api.get_config)(FakeHass(coord), conn, {"id": 1}))
    assert "lockout_until" in conn.payload
    assert conn.payload["lockout_until"] is None


# ── _serialize_zone_regions: the v1.60 payload shape ─────────────────


class _RegionCoord:
    def __init__(self, regions, yard_map=None):
        self.config = {"yard_map": yard_map} if yard_map else {}
        self._regions = regions

    @property
    def zone_regions(self):
        class _Store:
            def __init__(self, rs):
                self._rs = rs

            def all(self):
                return self._rs

        return _Store(self._regions)


def _region():
    import math

    from custom_components.complete_irrigation.zone_region import ZoneRegion

    lat, lon = 33.30, -111.80
    hl = 5.0 / 111_320.0
    hn = 5.0 / (111_320.0 * math.cos(math.radians(lat)))
    return ZoneRegion(
        id="r1",
        zone_entity_id="switch.back_yard_grass_blue",
        north=lat + hl,
        south=lat - hl,
        east=lon + hn,
        west=lon - hn,
    )


def test_regions_serialize_without_a_yard_map_and_omit_geometry():
    out = ws_api._serialize_zone_regions(_RegionCoord([_region()]))
    assert len(out) == 1
    assert out[0]["zone_entity_id"] == "switch.back_yard_grass_blue"
    assert "x0" not in out[0]  # no frame to project against — omitted, not faked


def test_regions_carry_derived_geometry_when_a_yard_map_exists():
    from custom_components.complete_irrigation.yard_map import bbox_from_center

    bb = bbox_from_center(33.30, -111.80, 60.0)
    ym = {"bbox": {"west": bb.west, "south": bb.south, "east": bb.east, "north": bb.north}}
    out = ws_api._serialize_zone_regions(_RegionCoord([_region()], yard_map=ym))
    r = out[0]
    for k in ("x0", "y0", "x1", "y1", "visible", "area_sqft"):
        assert k in r, f"missing {k}"
    assert r["visible"] is True
    assert 0.0 < r["x0"] < r["x1"] < 1.0


def test_region_geometry_is_unclamped_so_a_big_lawn_is_not_silently_shrunk():
    """A loop that runs past the edge of the photo must report its TRUE extent —
    clamping would be a confident lie about which ground the loop covers."""
    from custom_components.complete_irrigation.yard_map import bbox_from_center

    bb = bbox_from_center(33.30, -111.80, 4.0)  # frame much smaller than the region
    ym = {"bbox": {"west": bb.west, "south": bb.south, "east": bb.east, "north": bb.north}}
    r = ws_api._serialize_zone_regions(_RegionCoord([_region()], yard_map=ym))[0]
    assert r["x0"] < 0.0 or r["x1"] > 1.0
    assert r["visible"] is True


def test_no_regions_serializes_to_an_empty_list():
    assert ws_api._serialize_zone_regions(_RegionCoord([])) == []
