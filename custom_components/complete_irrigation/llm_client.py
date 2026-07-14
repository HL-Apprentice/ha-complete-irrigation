"""Pluggable LLM client (v1.41) — local model + optional external fallback.

The plant-brain features (species identification from a photo, species research
by name) call an OpenAI-compatible ``/v1/chat/completions`` endpoint. The default
is a local vision model (e.g. qwen2.5vl on a LAN GPU box): private and free, but a
small model — it often can't DIFFERENTIATE species or returns unparseable JSON.

This module lets the user attach a stronger EXTERNAL provider (Anthropic Claude,
xAI Grok, Google Gemini, or any OpenAI-compatible URL) and choose how it engages::

    llm_mode = "local"     -> local only (default; unchanged behavior)
               "external"  -> external only
               "fallback"  -> try local first, use external only when local fails
                              or returns an answer the caller can't use

Every provider here speaks the SAME OpenAI ``/v1/chat/completions`` shape, so one
code path + a Bearer key covers all of them. The API key lives in the config store
on the HA box (like any integration credential) and is NEVER logged or echoed.

``resolve_targets`` is pure (unit-tested); ``call_chat`` is the thin async POST
loop that walks the targets in order until one gives a usable answer.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LLM_MODES = ("local", "external", "fallback")

# Stable base URLs + a friendly model hint per provider. The URL is auto-filled in
# the UI when a provider is chosen; the MODEL is the user's to set — we never
# silently assume a model id (a wrong one would fail every call). "custom" = the
# user supplies both url + model for any other OpenAI-compatible server.
PROVIDERS: dict[str, dict[str, str]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "url": "https://api.anthropic.com/v1/chat/completions",
        "model_hint": "claude-sonnet-5",
    },
    "xai": {
        "label": "xAI (Grok)",
        "url": "https://api.x.ai/v1/chat/completions",
        "model_hint": "grok-4",
    },
    "google": {
        "label": "Google (Gemini)",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model_hint": "gemini-2.5-flash",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "url": "",
        "model_hint": "",
    },
}

# resp.content.read(n) reads UP TO n bytes; getting exactly the cap means there is
# almost certainly more -> treat as too large rather than parse a truncated body.
_MAX_RESP_BYTES = 1_000_001


@dataclass(frozen=True)
class LlmTarget:
    """One resolved endpoint we can POST a chat completion to."""

    label: str  # human label for logs/UI ("local", "Anthropic (Claude)")
    url: str
    model: str
    api_key: str = ""  # "" -> no Authorization header (local ollama / vLLM)
    kind: str = "local"  # "local" | "external"


@dataclass(frozen=True)
class LlmResult:
    ok: bool
    content: str = ""
    provider: str = ""  # label of the target that answered
    model: str = ""
    error: str = ""
    # per-target outcomes, for a probe report: (("local", "ok"), ("Claude", "HTTP 401"))
    attempts: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _local_target(config: dict[str, Any]) -> LlmTarget | None:
    url = str(config.get("vision_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    model = str(config.get("vision_model") or "").strip() or "default"
    return LlmTarget(label="local", url=url, model=model, api_key="", kind="local")


def _external_target(config: dict[str, Any]) -> LlmTarget | None:
    provider = str(config.get("llm_provider") or "").strip().lower()
    preset = PROVIDERS.get(provider)
    # url: an explicit override wins, else the provider preset's base URL.
    url = str(config.get("llm_external_url") or "").strip()
    if not url and preset:
        url = preset["url"]
    model = str(config.get("llm_external_model") or "").strip()
    key = str(config.get("llm_external_api_key") or "").strip()
    label = preset["label"] if preset else "External LLM"
    # Fully configured or nothing — a half-set external target is dropped, so
    # "fallback" with no key simply behaves like "local".
    if not url.startswith(("http://", "https://")) or not model or not key:
        return None
    return LlmTarget(label=label, url=url, model=model, api_key=key, kind="external")


def configured_targets(config: dict[str, Any]) -> list[LlmTarget]:
    """Every FULLY-configured endpoint (local and/or external), independent of
    ``llm_mode`` — for the "Test connection" button, so the user can confirm each
    endpoint works before choosing a mode. Order: local first, then external."""
    return [t for t in (_local_target(config), _external_target(config)) if t is not None]


def resolve_targets(config: dict[str, Any]) -> list[LlmTarget]:
    """Ordered list of endpoints to try, per ``llm_mode``. PURE + defensive: a
    target that isn't fully configured is silently dropped."""
    mode = str(config.get("llm_mode") or "local").strip().lower()
    if mode not in LLM_MODES:
        mode = "local"
    local = _local_target(config)
    external = _external_target(config)
    if mode == "local":
        picks = [local]
    elif mode == "external":
        picks = [external]
    else:  # fallback: local first, external as the safety net
        picks = [local, external]
    return [t for t in picks if t is not None]


async def call_chat(
    session: Any,
    targets: list[LlmTarget],
    messages: list[dict],
    *,
    timeout_s: float = 120,
    # Default sized for reasoning models (Gemini 2.5 / Claude / grok-reasoning):
    # their hidden "thinking" draws from the same completion budget, so a small
    # cap truncates the visible answer (finish_reason=length). 400 broke Gemini.
    max_tokens: int = 4000,
    temperature: float = 0.2,
    accept: Callable[[str], bool] | None = None,
) -> LlmResult:
    """POST an OpenAI chat completion to each target in order until one gives a
    usable answer. "Usable" = HTTP 200, OpenAI-shaped, non-empty content, and
    (if ``accept`` is given) ``accept(content)`` is True — the last lets a caller
    push a semantically-empty local answer down to the external provider.

    Never raises for a provider error; returns ``LlmResult(ok=False, ...)`` with a
    per-target attempts log. API keys are NEVER placed in the log or error text.
    """
    import aiohttp

    attempts: list[tuple[str, str]] = []
    for t in targets:
        headers = {"Content-Type": "application/json"}
        if t.api_key:
            headers["Authorization"] = f"Bearer {t.api_key}"
        body = _json.dumps(
            {
                "model": t.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        try:
            async with session.post(
                t.url,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    attempts.append((t.label, f"HTTP {resp.status}"))
                    continue
                raw = await resp.content.read(_MAX_RESP_BYTES)
        except Exception as err:  # any transport error -> try the next target
            attempts.append((t.label, f"unreachable: {str(err)[:80]}"))
            continue
        if len(raw) >= _MAX_RESP_BYTES:
            attempts.append((t.label, "response too large"))
            continue
        try:
            content = _json.loads(raw)["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError, TypeError):
            attempts.append((t.label, "reply not OpenAI-shaped"))
            continue
        if not isinstance(content, str) or not content.strip():
            attempts.append((t.label, "empty reply"))
            continue
        if accept is not None and not accept(content):
            attempts.append((t.label, "reply not usable"))
            continue
        attempts.append((t.label, "ok"))
        return LlmResult(
            ok=True,
            content=content,
            provider=t.label,
            model=t.model,
            attempts=tuple(attempts),
        )
    err = "; ".join(f"{lbl}: {msg}" for lbl, msg in attempts) or "no LLM endpoint configured"
    return LlmResult(ok=False, error=err, attempts=tuple(attempts))
