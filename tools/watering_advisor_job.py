#!/usr/bin/env python3
"""Watering-advisor job (v1.39) — the external LLM half of the smart scheduler.

Runs OFF the Home Assistant box, on your LLM host (any machine serving a local
OpenAI-compatible text model on localhost — deliberately not LAN-exposed).
Every run it:

  1. GETs the integration's health.json (schedules, per-plant need-vs-delivered
     incl. the user's INSTALLED drip emitters, the advisory daily plan, ETo);
  2. asks the local text LLM for a SMALL set of improvements, restricted to the
     two shapes the server-side rail accepts:
       * shift_time      — move a schedule's start (cooler hour, spread load)
       * emitter_change  — resize a plant's drip set toward its real need
  3. POSTs the raw reply to ``complete_irrigation.propose_watering_advice``.
     The INTEGRATION's rail (watering_advisor.validate_advice) bounds it —
     unknown ids, bad times, out-of-range values are dropped server-side.
     NOTHING IS APPLIED automatically: each surviving item shows in the panel
     with its own Apply button, wired to the same validated services the user
     drives manually.

ADVISORY ONLY — this job cannot change watering. Stdlib only; cron-friendly.

Config via env (all optional; sane defaults):
  WA_HEALTH_URL  http://<ha>:8123/api/complete_irrigation/health.json
  WA_HA_URL      http://<ha>:8123
  WA_TOKEN_FILE  ~/.config/watering-advisor/ha_token   (root-only file, ADMIN LLAT)
  WA_LLM_URL     http://127.0.0.1:8000/v1/chat/completions
  WA_LLM_MODEL   (server default when empty)
  WA_DRY_RUN     1 to print the advice without POSTing it
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


CFG = {
    "prompt_file": _env("WA_PROMPT_FILE", ""),
    "health_url": _env(
        "WA_HEALTH_URL", "http://homeassistant.local:8123/api/complete_irrigation/health.json"
    ),
    "ha_url": _env("WA_HA_URL", "http://homeassistant.local:8123"),
    "token_file": _env("WA_TOKEN_FILE", str(HOME / ".config/watering-advisor/ha_token")),
    "llm_url": _env("WA_LLM_URL", "http://127.0.0.1:8000/v1/chat/completions"),
    "llm_model": _env("WA_LLM_MODEL", ""),
    "dry_run": _env("WA_DRY_RUN", "") not in ("", "0", "false", "no"),
}

_SYSTEM = (
    "You are an irrigation scheduling advisor for a hot-arid (desert) yard. "
    "You may ONLY propose items of these two exact shapes, and only when the data "
    "supports them:\n"
    '  {"type":"shift_time","schedule_id":"<id from the data>","proposed_start":"HH:MM",'
    '"reason":"<=200 chars"}\n'
    '  {"type":"emitter_change","plant_id":"<id from the data>","proposed_count":int,'
    '"proposed_gph":number,"reason":"<=200 chars"}\n'
    "Guidance: prefer pre-dawn starts (04:00-06:00) in summer heat; avoid overlapping "
    "long runs; propose emitter changes ONLY for plants whose installed delivery is "
    "UNDER or OVER their need by more than 15 percent, moving toward the recommended "
    "set. Fewer, higher-confidence items beat many. Maximum 6 items. Reply with ONLY "
    'minified JSON: {"summary":"one or two sentences","items":[...]} — no prose.'
)


def _log(msg: str) -> None:
    print(f"[watering-advisor] {msg}", flush=True)


def _load_token() -> str | None:
    try:
        return Path(CFG["token_file"]).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: int = 180):
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST" if data else "GET"
    )
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def compact_context(health: dict) -> dict:
    """Boil health.json down to what the advisor needs (and nothing it doesn't)."""
    schedules = [
        {
            "schedule_id": s.get("id"),
            "name": s.get("name"),
            "zone": s.get("zone_entity_id"),
            "start": s.get("start_time"),
            "minutes": s.get("duration_minutes"),
            "enabled": s.get("enabled", True),
        }
        for s in (health.get("schedules") or [])[:40]
    ]
    loops = []
    for lp in (health.get("loops") or [])[:20]:
        plants = [
            {
                "plant_id": p.get("plant_id") or p.get("id"),
                "name": p.get("name"),
                "need_gal_week": p.get("need_gal_week"),
                "installed_delivered_gal_week": p.get("installed_delivered_gal_week"),
                "installed_status": p.get("installed_status"),
                "installed_pct_off": p.get("installed_pct_off"),
                "recommended_emitters": p.get("emitters"),
            }
            for p in (lp.get("plants") or [])[:30]
        ]
        loops.append({"zone": lp.get("loop_id") or lp.get("zone"), "plants": plants})
    return {
        "eto": health.get("eto"),
        "schedules": schedules,
        "loops": loops,
        "daily_plan": health.get("daily_plan"),
    }


def _system_prompt() -> str:
    """The embedded rules, optionally prefixed by the full canonical operations
    prompt (tools/LLM_OPERATIONS_PROMPT.md) via WA_PROMPT_FILE — the living
    gotcha/ground-truth document that makes a light model reliable."""
    if CFG["prompt_file"]:
        try:
            doc = Path(CFG["prompt_file"]).read_text(encoding="utf-8")
            return doc + "\n\n---\nJOB-SPECIFIC RULES (Section D applies):\n" + _SYSTEM
        except OSError as err:
            _log(f"prompt file unreadable ({err}); using embedded rules")
    return _SYSTEM


def ask_llm(context: dict) -> dict | None:
    body = json.dumps(
        {
            "model": CFG["llm_model"] or "default",
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": "Current irrigation data (JSON): " + json.dumps(context),
                },
            ],
            "temperature": 0.2,
            "max_tokens": 700,
        }
    ).encode("utf-8")
    try:
        raw = _http(CFG["llm_url"], data=body, headers={"Content-Type": "application/json"})
    except (urllib.error.URLError, OSError) as err:
        _log(f"LLM call failed: {err}")
        return None
    try:
        text = json.loads(raw)["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError):
        _log("LLM returned an unexpected envelope")
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def main() -> int:
    token = _load_token()
    if not token:
        _log(f"no HA token at {CFG['token_file']} — create a root-only file with an ADMIN LLAT")
        return 2
    try:
        health = json.loads(_http(CFG["health_url"], headers={"Authorization": f"Bearer {token}"}))
    except (urllib.error.URLError, OSError, ValueError) as err:
        _log(f"could not fetch health.json: {err}")
        return 2

    context = compact_context(health)
    if not context["schedules"]:
        _log("no schedules — nothing to advise on")
        return 0
    advice = ask_llm(context)
    if advice is None or not advice.get("items"):
        _log("no usable advice from the LLM this run")
        return 0
    if CFG["dry_run"]:
        _log(f"[dry-run] {json.dumps(advice)}")
        return 0
    url = CFG["ha_url"].rstrip("/") + "/api/services/complete_irrigation/propose_watering_advice"
    body = json.dumps({"advice": advice, "model": CFG["llm_model"] or "local-llm"}).encode("utf-8")
    try:
        _http(
            url,
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    except (urllib.error.URLError, OSError) as err:
        _log(f"propose_watering_advice POST failed: {err}")
        return 1
    _log(f"advice posted: {len(advice.get('items', []))} raw item(s); the rail decides the rest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
