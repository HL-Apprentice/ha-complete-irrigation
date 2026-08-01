"""LLM credential handling (v1.63).

Two leak paths existed, both narrow but real, and both ending somewhere the
user can see:

  * ``LlmTarget`` is a dataclass holding ``api_key``. With the default
    ``repr=True`` any stray ``f"{target}"`` or ``_LOGGER.debug("%s", target)``
    would print the key.
  * ``call_chat`` built its per-target failure note from ``str(err)``. aiohttp
    puts the request URL into most connector errors, and a "custom" provider
    may carry its credential in the query string. That note is joined into
    ``LlmResult.error``, which ``schedule_review`` LOGS and ``services`` raises
    into a ``vol.Invalid`` shown in the panel.

These tests pin both closures.
"""

from __future__ import annotations

from custom_components.complete_irrigation.llm_client import (
    LlmTarget,
    safe_error_text,
)

KEY = "sk-live-do-not-leak-abcdef123456"


# ── the dataclass must not print its own key ─────────────────────────


def test_repr_of_a_target_does_not_contain_the_key():
    t = LlmTarget(label="Anthropic (Claude)", url="https://api.example/v1", model="m", api_key=KEY)
    assert KEY not in repr(t)
    assert KEY not in f"{t}"
    # _LOGGER.debug("%s", target) renders via str(); dataclasses route str()
    # to __repr__, so this is the path a stray log line would take.
    assert KEY not in str(t)


def test_repr_still_shows_the_useful_fields():
    """Redaction must not make the target useless in a log line."""
    t = LlmTarget(label="local", url="http://gpu.lan:8000/v1", model="qwen", api_key="")
    r = repr(t)
    for keep in ("local", "gpu.lan", "qwen"):
        assert keep in r, f"{keep} should still be visible for debugging"


def test_the_key_is_still_readable_by_the_code_that_needs_it():
    """repr=False hides it from output, it must NOT hide it from the caller."""
    t = LlmTarget(label="x", url="https://a/v1", model="m", api_key=KEY)
    assert t.api_key == KEY


# ── transport errors must not carry credentials out ──────────────────


def test_a_key_in_the_query_string_is_stripped():
    err = OSError("Cannot connect to https://api.example.com/v1/chat?key=" + KEY)
    out = safe_error_text(err, "")
    assert KEY not in out
    assert "<redacted>" in out


def test_userinfo_credentials_in_a_url_are_stripped():
    err = OSError("Cannot connect to https://user:hunter2@api.example.com/v1/chat")
    out = safe_error_text(err, "")
    assert "hunter2" not in out
    assert "user:" not in out


def test_the_known_key_is_stripped_even_outside_a_url():
    err = OSError(f"auth failed for token {KEY} on attempt 1")
    out = safe_error_text(err, KEY)
    assert KEY not in out
    assert "***" in out


def test_redaction_runs_before_truncation():
    """Truncating first would cut the key in half and still leak its front."""
    err = OSError("x" * 60 + " https://api.example.com/v1?key=" + KEY)
    out = safe_error_text(err, KEY)
    for n in range(8, len(KEY) + 1):
        assert KEY[:n] not in out, f"leaked the first {n} chars of the key"


def test_an_ordinary_error_is_left_readable():
    out = safe_error_text(TimeoutError("Connection timeout to host gpu.lan:8000"), "")
    assert "timeout" in out.lower()
    assert "gpu.lan" in out


def test_path_is_kept_when_only_the_query_is_dropped():
    err = OSError("Cannot connect to https://api.example.com/v1/chat/completions?key=abc")
    out = safe_error_text(err, "")
    assert "/v1/chat/completions" in out, "the useful part of the URL should survive"
    assert "key=abc" not in out


def test_output_stays_bounded():
    assert len(safe_error_text(OSError("y" * 5000), "")) <= 80


def test_empty_key_is_not_treated_as_a_substring_to_redact():
    """An empty api_key must not turn every character into ***."""
    out = safe_error_text(OSError("plain failure"), "")
    assert out == "plain failure"
