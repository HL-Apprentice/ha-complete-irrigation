#!/usr/bin/env python3
"""Plant vision-health job (v1.33) — the external half of the biannual health check.

Runs OFF the Home Assistant box (on the NAS with the vision GPU, or the LLM host).
Every run it:

  1. GETs the integration's health.json and finds plants whose ``due_for_review`` is
     true (photo history exists AND the last assessment is older than the interval);
  2. for each, fetches the plant's ``review`` photos (latest + an older baseline) from
     Home Assistant's ``/local/`` store;
  3. asks a LOCAL vision model (an OpenAI-compatible /v1/chat/completions endpoint —
     e.g. Qwen2.5-VL served by vLLM/Ollama/mlx on the RTX 5060) to compare them and
     return a structured verdict;
  4. POSTs that raw verdict to the ``complete_irrigation.set_plant_health`` service.
     The INTEGRATION's rail (vision_health.validate_verdict) bounds it before storage —
     this job never has to be trusted to behave; the deterministic guardrail lives
     server-side, exactly like the daily-plan advisor.

ADVISORY ONLY — nothing here touches watering. Stdlib only, so it drops onto any host
with python3 and no pip installs. Designed to be run from cron / a launchd timer, e.g.
weekly (the ``due_for_review`` gate makes each plant get looked at ~biannually).

Config via env (all optional; sane defaults):
  VH_HEALTH_URL   http://<ha>:8123/api/complete_irrigation/health.json
  VH_HA_URL       http://<ha>:8123            (base, for /local photos + the service call)
  VH_TOKEN_FILE   ~/.config/vision-health/ha_token   (root-only file, an ADMIN LLAT)
  VH_VISION_URL   http://127.0.0.1:8000/v1/chat/completions
  VH_VISION_MODEL Qwen2.5-VL-7B-Instruct
  VH_MAX_PLANTS   4          (cap per run so a big yard doesn't hammer the GPU)
  VH_DRY_RUN      1 to print verdicts without POSTing them
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int_env(name: str, default: int) -> int:
    """Like _env but tolerant: a non-numeric VH_MAX_PLANTS must not crash the whole
    job at import with an unhandled ValueError — fall back to the default instead."""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


CFG = {
    "health_url": _env(
        "VH_HEALTH_URL", "http://homeassistant.local:8123/api/complete_irrigation/health.json"
    ),
    "ha_url": _env("VH_HA_URL", "http://homeassistant.local:8123"),
    "token_file": _env("VH_TOKEN_FILE", str(HOME / ".config/vision-health/ha_token")),
    "vision_url": _env("VH_VISION_URL", "http://127.0.0.1:8000/v1/chat/completions"),
    "vision_model": _env("VH_VISION_MODEL", "Qwen2.5-VL-7B-Instruct"),
    "max_plants": _int_env("VH_MAX_PLANTS", 4),
    "dry_run": _env("VH_DRY_RUN", "") not in ("", "0", "false", "no"),
}

_PROMPT = (
    "You are a horticulture assistant reviewing photos of a single plant over time. "
    "The FIRST image is an earlier BASELINE (may be absent); the LAST image is the LATEST. "
    "Assess the plant's current health and how it has CHANGED since the baseline. "
    "Be conservative and specific; do NOT give watering commands or mention any device, "
    "switch, zone, or service — only plain-language plant-care guidance. "
    "Reply with ONLY minified JSON, no prose:\n"
    '{"health_state":"thriving|healthy|stressed|declining|unknown",'
    '"confidence":0.0-1.0,'
    '"changes_since_last":"one or two sentences",'
    '"concerns":["short phrases"],'
    '"suggested_care":["short plain-language tips"]}'
)


def _log(msg: str) -> None:
    print(f"[vision-health] {msg}", flush=True)


def _load_token() -> str | None:
    try:
        return Path(CFG["token_file"]).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow 3xx redirects (a redirect becomes an HTTPError the callers
    swallow). SSRF guard: a /local/ photo path must never bounce the request to an
    internal/metadata URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST" if data else "GET"
    )
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def fetch_health(token: str) -> dict:
    raw = _http(CFG["health_url"], headers={"Authorization": f"Bearer {token}"})
    return json.loads(raw)


def fetch_image_b64(local_path: str, token: str) -> str | None:
    """Fetch a /local/... photo from HA and return base64 (or None on failure)."""
    if not local_path or not local_path.startswith("/local/"):
        return None
    # Defence-in-depth: the host is already fixed (CFG['ha_url']) and redirects are
    # refused, but never let a crafted health.json path use "../" to climb out of
    # /local/ into another admin-token-authorised HA endpoint. The rail architecture's
    # own premise is that this job must not trust the data it is handed.
    if ".." in local_path:
        _log(f"  refusing suspicious photo path {local_path}")
        return None
    url = CFG["ha_url"].rstrip("/") + local_path.split("?", 1)[0]
    try:
        raw = _http(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    except (urllib.error.URLError, OSError) as err:
        _log(f"  photo fetch failed {local_path}: {err}")
        return None
    return base64.b64encode(raw).decode("ascii")


def plant_context(p: dict) -> str:
    """v1.36 — optional per-plant context appended to the prompt: species, optimal
    lux range, and the latest light-survey verdict. All values come from the
    integration's health.json (already rail-bounded); kept short + plain-text."""
    bits: list[str] = []
    species = str(p.get("species") or "").strip()
    if species:
        bits.append(f"The plant is a {species[:120]}.")
    rng = p.get("lux_range")
    if isinstance(rng, dict) and rng.get("low") is not None:
        bits.append(f"Its preferred light range is {rng['low']}-{rng['high']} lux.")
    light = p.get("light")
    if isinstance(light, dict) and light.get("verdict") in ("too_low", "too_high", "optimal"):
        word = {
            "too_low": "LESS light than it prefers",
            "too_high": "MORE light than it prefers",
            "optimal": "light within its preferred range",
        }[light["verdict"]]
        bits.append(f"A recent light survey at its spot measured {word}.")
    if not bits:
        return ""
    return " Context: " + " ".join(bits)


def ask_vision(images_b64: list[str], context: str = "") -> dict | None:
    """Call the local vision model; return the parsed JSON verdict or None."""
    content = [{"type": "text", "text": _PROMPT + context}]
    for b64 in images_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    body = json.dumps(
        {
            "model": CFG["vision_model"],
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": 500,
        }
    ).encode("utf-8")
    try:
        raw = _http(CFG["vision_url"], data=body, headers={"Content-Type": "application/json"})
    except (urllib.error.URLError, OSError) as err:
        _log(f"  vision model call failed: {err}")
        return None
    try:
        text = json.loads(raw)["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError):
        _log("  vision model returned an unexpected envelope")
        return None
    return _extract_json(text)


def _extract_json(text: str) -> dict | None:
    """Pull the first {...} object out of the model's text (tolerates code fences)."""
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def post_verdict(token: str, plant_id: str, verdict: dict) -> bool:
    url = CFG["ha_url"].rstrip("/") + "/api/services/complete_irrigation/set_plant_health"
    body = json.dumps(
        {"plant_id": plant_id, "verdict": verdict, "model": CFG["vision_model"]}
    ).encode("utf-8")
    try:
        _http(
            url,
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        return True
    except (urllib.error.URLError, OSError) as err:
        _log(f"  set_plant_health POST failed for {plant_id}: {err}")
        return False


def main() -> int:
    token = _load_token()
    if not token:
        _log(f"no HA token at {CFG['token_file']} — create a root-only file with an ADMIN LLAT")
        return 2
    try:
        health = fetch_health(token)
    except (urllib.error.URLError, OSError, ValueError) as err:
        _log(f"could not fetch health.json: {err}")
        return 2

    plants = health.get("plant_health") or []
    due = [p for p in plants if p.get("due_for_review") and p.get("review")]
    _log(f"{len(due)} plant(s) due for review (of {len(plants)})")
    if not due:
        return 0

    reviewed = 0
    for p in due[: CFG["max_plants"]]:
        pid, name = p.get("plant_id"), p.get("name", "?")
        review = p.get("review") or {}
        baseline = (review.get("baseline") or {}).get("path")
        latest = (review.get("latest") or {}).get("path")
        imgs = [fetch_image_b64(x, token) for x in (baseline, latest) if x]
        imgs = [x for x in imgs if x]
        if not imgs:
            _log(f"{name} ({pid}): no fetchable photos — skipping")
            continue
        verdict = ask_vision(imgs, context=plant_context(p))
        if verdict is None:
            _log(f"{name} ({pid}): no usable verdict — skipping")
            continue
        if CFG["dry_run"]:
            _log(f"{name} ({pid}) [dry-run]: {json.dumps(verdict)}")
            reviewed += 1
            continue
        if post_verdict(token, pid, verdict):
            _log(f"{name} ({pid}): {verdict.get('health_state', '?')} — stored")
            reviewed += 1
    _log(f"done — {reviewed} plant(s) assessed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
