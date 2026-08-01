"""ICS feed escaping (v1.63) — ical_view.py had no test of its own.

The feed is assembled by joining lines with CRLF, so any value carrying a
carriage return, newline, comma or semicolon can forge new iCalendar
properties — or a whole new VEVENT — inside someone's subscribed calendar.

`_ics_escape` is the single defence. These tests pin it, and pin the fact that
every value interpolated into the feed goes through it, including `schedule_id`
in the UID line (Schedule.from_dict reads `id` straight from .storage with no
validation, so a corrupted store must not be able to reach the feed unescaped).
"""

from __future__ import annotations

import pytest

# ical_view imports HomeAssistantView, so this is an HA-tier test
# (skips without HA installed; runs in the pytest-full CI job).
pytest.importorskip("homeassistant.components.http")

from custom_components.complete_irrigation.ical_view import _ics_escape


def test_newlines_cannot_forge_a_new_property():
    out = _ics_escape("Front Lawn\nSUMMARY:Injected")
    assert "\n" not in out
    assert out == "Front Lawn\\nSUMMARY:Injected"


def test_carriage_returns_are_dropped_entirely():
    """CRLF is the actual line separator in the assembled document."""
    out = _ics_escape("Lawn\r\nBEGIN:VEVENT")
    assert "\r" not in out
    assert "\r\n" not in out


def test_a_whole_forged_event_is_neutralised():
    payload = "Lawn\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Fake\r\nEND:VEVENT"
    out = _ics_escape(payload)
    # The document separates lines with a REAL CRLF. After escaping there is no
    # real CR or LF left, so this whole payload stays one physical line and a
    # parser sees one text value — not five properties.
    assert "\r" not in out
    assert "\n" not in out
    # \r is dropped as a control char; \n becomes the two-character sequence
    # backslash-n, which is the RFC 5545 encoding and is inert.
    assert out.count("\\n") == 4
    assert out == "Lawn\\nEND:VEVENT\\nBEGIN:VEVENT\\nSUMMARY:Fake\\nEND:VEVENT"


def test_structural_characters_are_escaped():
    assert _ics_escape("a,b") == "a\\,b"
    assert _ics_escape("a;b") == "a\\;b"
    assert _ics_escape("a\\b") == "a\\\\b"


def test_backslash_is_escaped_first_so_it_is_not_double_counted():
    """If ',' were escaped before '\\', the inserted backslash would itself be
    escaped again and the value would corrupt."""
    assert _ics_escape("\\,") == "\\\\\\,"


def test_control_characters_are_stripped():
    out = _ics_escape("Lawn\x00\x07\tzone")
    for ch in ("\x00", "\x07", "\t"):
        assert ch not in out
    assert out == "Lawnzone"


def test_ordinary_text_is_untouched():
    assert _ics_escape("Back Yard Grass - Blue") == "Back Yard Grass - Blue"


def test_unicode_survives():
    assert _ics_escape("Jardín 🌱") == "Jardín 🌱"


def test_every_interpolated_value_in_the_feed_is_escaped():
    """Source-level guard: a future edit that drops _ics_escape from one of the
    value lines would reintroduce feed injection, and no runtime test would
    necessarily catch it."""
    import inspect

    from custom_components.complete_irrigation import ical_view

    src = inspect.getsource(ical_view.IrrigationCalendarICSView.get)
    unescaped = []
    for line in src.split("\n"):
        s = line.strip()
        # the value-bearing ICS property lines built with an f-string
        if not s.startswith('f"') and not s.startswith('"'):
            continue
        for prop in ("UID:", "SUMMARY:", "DESCRIPTION:"):
            if prop in s and "_ics_escape" not in s:
                unescaped.append(s[:70])
    assert unescaped == [], f"ICS value interpolated without _ics_escape: {unescaped}"
