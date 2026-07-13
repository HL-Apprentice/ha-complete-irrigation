"""Tests for the pluggable LLM client (local + external fallback)."""

from __future__ import annotations

import json

import pytest

from custom_components.complete_irrigation.llm_client import (
    PROVIDERS,
    call_chat,
    resolve_targets,
)

LOCAL = {"vision_url": "http://nas:11434/v1/chat/completions", "vision_model": "qwen2.5vl:7b"}
EXT = {
    "llm_provider": "anthropic",
    "llm_external_model": "claude-sonnet-5",
    "llm_external_api_key": "test-key",
}


# ── resolve_targets (pure) ──────────────────────────────────────────


def test_default_mode_is_local_only():
    targets = resolve_targets(LOCAL)
    assert [t.label for t in targets] == ["local"]
    assert targets[0].api_key == ""
    assert targets[0].kind == "local"


def test_local_without_url_yields_nothing():
    assert resolve_targets({"vision_model": "x"}) == []


def test_external_mode_uses_provider_preset_url():
    cfg = {**LOCAL, **EXT, "llm_mode": "external"}
    targets = resolve_targets(cfg)
    assert len(targets) == 1
    t = targets[0]
    assert t.kind == "external"
    assert t.url == PROVIDERS["anthropic"]["url"]
    assert t.model == "claude-sonnet-5"
    assert t.api_key == "test-key"


def test_fallback_mode_is_local_then_external():
    cfg = {**LOCAL, **EXT, "llm_mode": "fallback"}
    assert [t.kind for t in resolve_targets(cfg)] == ["local", "external"]


def test_fallback_with_incomplete_external_degrades_to_local():
    # No API key -> external target is dropped, fallback behaves like local.
    cfg = {**LOCAL, "llm_mode": "fallback", "llm_provider": "xai", "llm_external_model": "grok-4"}
    assert [t.label for t in resolve_targets(cfg)] == ["local"]


def test_custom_provider_requires_explicit_url():
    # "custom" has no preset URL -> needs llm_external_url.
    cfg = {
        "llm_mode": "external",
        "llm_provider": "custom",
        "llm_external_model": "my-model",
        "llm_external_api_key": "k",
    }
    assert resolve_targets(cfg) == []
    cfg["llm_external_url"] = "https://my-llm.example/v1/chat/completions"
    targets = resolve_targets(cfg)
    assert targets and targets[0].url == "https://my-llm.example/v1/chat/completions"


def test_url_override_beats_provider_preset():
    cfg = {**EXT, "llm_mode": "external", "llm_external_url": "https://proxy/v1/chat/completions"}
    assert resolve_targets(cfg)[0].url == "https://proxy/v1/chat/completions"


def test_unknown_mode_falls_back_to_local():
    assert [t.label for t in resolve_targets({**LOCAL, "llm_mode": "bogus"})] == ["local"]


# ── call_chat (fake aiohttp session) ────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeContent:
    def __init__(self, body: bytes):
        self._body = body

    async def read(self, n: int) -> bytes:
        return self._body[:n]


class _FakeSession:
    """Records each POST and returns queued responses (or raises) in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # (url, headers, parsed_body)

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, headers or {}, json.loads(data)))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _envelope(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def _targets(cfg):
    return resolve_targets(cfg)


@pytest.mark.asyncio
async def test_call_chat_local_success_no_auth_header():
    sess = _FakeSession([_FakeResp(200, _envelope("hello"))])
    res = await call_chat(sess, _targets(LOCAL), [{"role": "user", "content": "hi"}])
    assert res.ok and res.content == "hello" and res.provider == "local"
    # local target sends no Authorization header
    assert "Authorization" not in sess.calls[0][1]


@pytest.mark.asyncio
async def test_call_chat_external_sends_bearer():
    cfg = {**EXT, "llm_mode": "external"}
    sess = _FakeSession([_FakeResp(200, _envelope("hi from claude"))])
    res = await call_chat(sess, _targets(cfg), [{"role": "user", "content": "hi"}])
    assert res.ok and res.provider == "Anthropic (Claude)"
    assert sess.calls[0][1]["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_call_chat_falls_back_on_local_http_error():
    cfg = {**LOCAL, **EXT, "llm_mode": "fallback"}
    sess = _FakeSession([_FakeResp(500, b""), _FakeResp(200, _envelope("rescued"))])
    res = await call_chat(sess, _targets(cfg), [{"role": "user", "content": "hi"}])
    assert res.ok and res.content == "rescued" and res.provider == "Anthropic (Claude)"
    assert len(sess.calls) == 2  # local tried first, then external


@pytest.mark.asyncio
async def test_call_chat_falls_back_when_accept_rejects_local():
    cfg = {**LOCAL, **EXT, "llm_mode": "fallback"}
    sess = _FakeSession([_FakeResp(200, _envelope("junk")), _FakeResp(200, _envelope("{ok}"))])
    res = await call_chat(
        sess,
        _targets(cfg),
        [{"role": "user", "content": "hi"}],
        accept=lambda c: c.startswith("{"),
    )
    assert res.ok and res.content == "{ok}" and res.provider == "Anthropic (Claude)"


@pytest.mark.asyncio
async def test_call_chat_all_fail_reports_each_attempt():
    cfg = {**LOCAL, **EXT, "llm_mode": "fallback"}
    sess = _FakeSession([RuntimeError("conn refused"), _FakeResp(401, b"")])
    res = await call_chat(sess, _targets(cfg), [{"role": "user", "content": "hi"}])
    assert not res.ok
    labels = dict(res.attempts)
    assert "unreachable" in labels["local"]
    assert labels["Anthropic (Claude)"] == "HTTP 401"


@pytest.mark.asyncio
async def test_call_chat_no_targets():
    res = await call_chat(_FakeSession([]), [], [{"role": "user", "content": "hi"}])
    assert not res.ok and "no LLM endpoint" in res.error


@pytest.mark.asyncio
async def test_call_chat_rejects_oversized_response():
    from custom_components.complete_irrigation.llm_client import _MAX_RESP_BYTES

    sess = _FakeSession([_FakeResp(200, b"x" * _MAX_RESP_BYTES)])
    res = await call_chat(sess, _targets(LOCAL), [{"role": "user", "content": "hi"}])
    assert not res.ok
    assert dict(res.attempts)["local"] == "response too large"


@pytest.mark.asyncio
async def test_call_chat_rejects_non_openai_shape():
    sess = _FakeSession([_FakeResp(200, json.dumps({"nope": 1}).encode())])
    res = await call_chat(sess, _targets(LOCAL), [{"role": "user", "content": "hi"}])
    assert not res.ok
    assert dict(res.attempts)["local"] == "reply not OpenAI-shaped"


@pytest.mark.asyncio
async def test_call_chat_rejects_empty_content():
    sess = _FakeSession([_FakeResp(200, _envelope("   "))])
    res = await call_chat(sess, _targets(LOCAL), [{"role": "user", "content": "hi"}])
    assert not res.ok
    assert dict(res.attempts)["local"] == "empty reply"


def test_local_target_defaults_model_when_blank():
    targets = resolve_targets({"vision_url": "http://nas:11434/v1/chat/completions"})
    assert targets and targets[0].model == "default"


def test_external_dropped_when_model_missing_but_key_present():
    cfg = {
        "llm_mode": "external",
        "llm_provider": "anthropic",
        "llm_external_api_key": "k",  # key present, model absent -> dropped
    }
    assert resolve_targets(cfg) == []
