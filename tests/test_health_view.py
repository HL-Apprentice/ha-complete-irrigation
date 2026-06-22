"""v1.26 — health.json miss-detection (the LLM monitor's deterministic floor).

Tests the pure helpers in health_view: _detect_misses (aborted / skipped / short
within 24 h) and _cadence_summary. health_view imports HomeAssistantView, so this
is an HA-tier test (skips without HA installed; runs in CI).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant.components.http")

from custom_components.complete_irrigation.health_view import (
    _cadence_summary,
    _detect_misses,
)

NOW = datetime(2026, 6, 21, 21, 0, tzinfo=UTC)


def _rec(status, started, req=15, act=15, reason="", zone="switch.bird_bath", name="Bird Bath"):
    return SimpleNamespace(
        started_at=started.isoformat(),
        status=status,
        reason=reason,
        requested_minutes=req,
        actual_minutes=act,
        zone_entity_id=zone,
        zone_name=name,
    )


def test_detects_aborted_skipped_and_short_within_window():
    history = [
        _rec(
            "aborted",
            NOW - timedelta(hours=1),
            req=15,
            act=8,
            reason="switch turned off externally",
        ),
        _rec("completed", NOW - timedelta(hours=2), req=60, act=30),  # short: 30 < 0.8*60
        _rec("skipped", NOW - timedelta(hours=3), reason="moisture > 95%"),
        _rec("completed", NOW - timedelta(hours=4), req=15, act=15),  # healthy
        _rec("aborted", NOW - timedelta(hours=30), req=15, act=5),  # too old (>24h)
    ]
    issues, aborted, skipped, short = _detect_misses(history, NOW)
    assert (aborted, skipped, short) == (1, 1, 1)
    assert len(issues) == 3
    assert any("ABORTED" in i and "Bird Bath" in i for i in issues)
    assert any("SHORT" in i for i in issues)
    assert any("SKIPPED" in i for i in issues)


def test_completed_full_run_is_not_short():
    issues, a, s, sh = _detect_misses(
        [_rec("completed", NOW - timedelta(hours=1), req=15, act=15)], NOW
    )
    assert issues == [] and (a, s, sh) == (0, 0, 0)


def test_just_under_threshold_counts_as_short():
    # 80% exactly is NOT short (act < req*0.8 is strict); 79% is.
    not_short = _detect_misses([_rec("completed", NOW - timedelta(hours=1), req=100, act=80)], NOW)
    assert not_short[0] == []
    is_short = _detect_misses([_rec("completed", NOW - timedelta(hours=1), req=100, act=79)], NOW)
    assert is_short[3] == 1


def test_cadence_summary_variants():
    assert _cadence_summary(SimpleNamespace(mode="interval_hours", interval_hours=4)) == "every 4h"
    assert (
        _cadence_summary(SimpleNamespace(mode="interval", interval_days=3, interval_hours=None))
        == "every 3d"
    )
    assert (
        _cadence_summary(
            SimpleNamespace(
                mode="weekdays", weekdays=(0, 2, 4), interval_hours=None, interval_days=None
            )
        )
        == "weekly: Mon,Wed,Fri"
    )
