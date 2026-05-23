"""Tests for in_quiet_hours pure logic + notify-target domain validation."""

from __future__ import annotations

from datetime import datetime

from custom_components.complete_irrigation.notifications import (
    _coerce_target_list,
    in_quiet_hours,
)


def test_quiet_hours_wrap_around_midnight():
    """Default 22:00-07:00 covers late night and early morning."""
    assert in_quiet_hours(datetime(2026, 5, 18, 23, 0), "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 5, 19, 3, 0), "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 5, 19, 6, 59), "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 5, 19, 7, 0), "22:00", "07:00") is False
    assert in_quiet_hours(datetime(2026, 5, 19, 12, 0), "22:00", "07:00") is False
    assert in_quiet_hours(datetime(2026, 5, 19, 21, 59), "22:00", "07:00") is False


def test_quiet_hours_no_wrap():
    """Same-day quiet window (e.g. 09:00-17:00)."""
    assert in_quiet_hours(datetime(2026, 5, 18, 10, 0), "09:00", "17:00") is True
    assert in_quiet_hours(datetime(2026, 5, 18, 17, 0), "09:00", "17:00") is False
    assert in_quiet_hours(datetime(2026, 5, 18, 8, 59), "09:00", "17:00") is False


def test_quiet_hours_edge_at_start():
    """Exactly at start → inside the window."""
    assert in_quiet_hours(datetime(2026, 5, 18, 22, 0), "22:00", "07:00") is True


# ── notify-target domain allowlist (v1.15 security) ────────────────────


def test_coerce_target_list_accepts_valid_notify_services():
    out = _coerce_target_list(["notify.mobile_app_pete", "notify.alexa_media"])
    assert out == ["notify.mobile_app_pete", "notify.alexa_media"]


def test_coerce_target_list_rejects_non_notify_domain():
    """Without this guard, set_notification_config could install arbitrary
    domain.service callouts that fire on every notification."""
    out = _coerce_target_list(
        [
            "notify.mobile_app_pete",
            "shell_command.curl_exfil",  # attacker-supplied
            "script.evil",
            "homeassistant.restart",
        ]
    )
    assert out == ["notify.mobile_app_pete"]


def test_coerce_target_list_rejects_missing_service():
    assert _coerce_target_list(["notify."]) == []
    assert _coerce_target_list(["notify"]) == []
    assert _coerce_target_list([""]) == []
    assert _coerce_target_list(["   "]) == []


def test_coerce_target_list_dedupes_and_strips():
    out = _coerce_target_list(
        ["notify.mobile_app_pete", "  notify.mobile_app_pete  ", "notify.foo"]
    )
    assert out == ["notify.mobile_app_pete", "notify.foo"]


def test_coerce_target_list_handles_comma_or_newline_string():
    raw = "notify.mobile_app_pete, notify.foo\nnotify.bar"
    assert _coerce_target_list(raw) == [
        "notify.mobile_app_pete",
        "notify.foo",
        "notify.bar",
    ]
    # And it still filters invalid domains when coming via string
    raw2 = "notify.ok\nshell_command.bad"
    assert _coerce_target_list(raw2) == ["notify.ok"]


def test_coerce_target_list_handles_none_and_garbage():
    assert _coerce_target_list(None) == []
    assert _coerce_target_list(42) == []
    assert _coerce_target_list([None, 42, "notify.ok"]) == ["notify.ok"]
