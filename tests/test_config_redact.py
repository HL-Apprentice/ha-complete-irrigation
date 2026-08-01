"""Config redaction (v1.63) — secrets must fail CLOSED.

The old code cloned the config and popped three known keys, so any NEW secret
field was shipped to every browser session until a human remembered to add
another pop. The list had already grown three times reactively. These tests
pin the replacement: unknown-but-secret-looking keys are stripped by default.
"""

from __future__ import annotations

from custom_components.complete_irrigation.config_redact import is_secret_key, redact_config


def test_the_three_historically_popped_keys_are_still_stripped():
    cfg = {
        "llm_external_api_key": "sk-abc",
        "plantnet_api_key": "pn-abc",
        "perenual_api_key": "pe-abc",
    }
    out = redact_config(cfg)
    for k in cfg:
        assert k not in out, f"{k} leaked"
        assert out[f"{k}_set"] is True


def test_a_secret_field_nobody_has_added_yet_is_stripped_anyway():
    """THE point of the change: this is the failure the deny-list had."""
    future = {
        "mqtt_password": "hunter2",
        "s3_secret_access_key": "x",
        "webhook_url": "https://hooks.example/T/abc/secret",
        "oauth_client_secret": "y",
        "refresh_token": "z",
        "basic_auth": "user:pass",
        "some_bearer": "abc",
        "device_credential": "c",
        "ssh_private_key": "-----BEGIN",
    }
    out = redact_config(future)
    for k in future:
        assert k not in out, f"{k} would have leaked to the browser"
        assert out[f"{k}_set"] is True


def test_ordinary_config_is_untouched():
    cfg = {
        "zones": {"switch.a": {"min": 20}},
        "rain_sensor": "sensor.rain",
        "notify_targets": ["notify.mobile"],
        "drip_efficiency": 0.9,
        "admin_only_services": False,
        "yard_map": {"span_m": 60},
        "advisor_preferences": [{"text": "keep trees late"}],
    }
    assert redact_config(cfg) == cfg


def test_empty_and_whitespace_values_report_not_set():
    out = redact_config({"llm_external_api_key": "", "plantnet_api_key": "   "})
    assert out["llm_external_api_key_set"] is False
    assert out["plantnet_api_key_set"] is False


def test_none_reports_not_set():
    assert redact_config({"some_api_key": None})["some_api_key_set"] is False


def test_the_set_flags_themselves_are_never_re_redacted():
    # A payload that already carries *_set booleans must survive a second pass.
    once = redact_config({"llm_external_api_key": "sk"})
    twice = redact_config(once)
    assert twice == once
    assert "llm_external_api_key_set" in twice


def test_is_secret_key_classification():
    for k in (
        "api_key",
        "openai_apikey",
        "my_secret",
        "db_password",
        "auth_header",
        "access_token",
        "client_secret",
        "ssh_private_key",
        "webhook_url",
    ):
        assert is_secret_key(k), k
    for k in ("zones", "rain_sensor", "span_m", "llm_external_url", "drip_efficiency"):
        assert not is_secret_key(k), k


def test_non_dict_input_is_safe():
    for bad in (None, [], "x", 42):
        assert redact_config(bad) == {}


def test_redaction_never_mutates_the_caller_s_config():
    """The live coordinator config is passed in — redacting must not damage it."""
    cfg = {"llm_external_api_key": "sk-secret", "zones": {}}
    snapshot = dict(cfg)
    redact_config(cfg)
    assert cfg == snapshot
    assert cfg["llm_external_api_key"] == "sk-secret"  # still usable server-side
