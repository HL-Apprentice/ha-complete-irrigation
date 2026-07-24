"""Schedule review orchestration (v1.56).

One entry point — ``review_schedule`` — shared by the add/edit service handlers
(``new_schedule_id`` set) and the nightly safety sweep (``new_schedule_id=None``).
It detects conflicts on the one-zone controller and then EITHER asks the configured
LLM for propose-only shift/split fixes, OR (no LLM) notifies the user to adjust
manually. It NEVER actuates and NEVER blocks — the never-drop resolver stays the
live failsafe. This module holds the HA glue; the detection/validation logic lives
in the pure schedule_conflicts / schedule_advisor / schedule_split modules.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from .schedule_split import effective_min_chunk, merge_chunk_defaults

_LOGGER = logging.getLogger(__name__)

_PROMPT = """You are an irrigation SCHEDULING assistant. The controller runs ONE zone \
at a time, so schedules whose run windows overlap collide. Propose the SMALLEST set of \
adjustments that makes everything fit, keeping the schedule as pure as possible.

Rules (hard):
- NEVER drop a run and NEVER change a run's total minutes. Everything still waters fully.
- Keep ESSENTIAL schedules as close to their time and UN-split as possible. Disrupt \
NON-ESSENTIAL schedules FIRST (move them, or split them into pieces that fit the gaps).
- Only split when moving alone can't fit it. A split's parts must sum EXACTLY to the \
schedule's duration, and each part must be >= that schedule's min_chunk minutes.
- Pulling a run EARLIER is fine. Prefer the least total disruption.

Return ONLY JSON: {"items":[...],"summary":"..."} where each item is either
 {"type":"shift","schedule_id":"<id>","proposed_start":"HH:MM","reason":"..."} or
 {"type":"split","schedule_id":"<id>","parts":[{"start":"HH:MM","minutes":N},...],"reason":"..."}.
If nothing needs changing, return {"items":[],"summary":"no change needed"}.

SCHEDULES:
{SCHEDULES}

DETECTED CONFLICTS:
{CONFLICTS}
"""


_CHAT_PROMPT = """You are the user's irrigation SCHEDULING assistant. Answer their \
question about their schedules conversationally and concisely (2-5 sentences). The \
controller runs ONE zone at a time, so overlapping run windows collide; ESSENTIAL runs \
are protected while NON-ESSENTIAL runs are moved or split to fit around them, and nothing \
is ever dropped.

Only if the user is asking you to CHANGE the schedule, ALSO include propose-only \
adjustments -- they review and apply them; you never change anything directly. Hard \
rules: never drop a run, never change a run's total minutes, a split's parts sum EXACTLY \
to the duration and each part >= that schedule's min_chunk. When you include items, your \
reply MUST say what you are proposing in plain words (e.g. "I've proposed moving Citrus \
to 03:30") -- the reply is shown next to an Apply card carrying those items.

Return ONLY JSON: {"reply":"<your conversational answer>","items":[...],"summary":"..."} \
where each item is
 {"type":"shift","schedule_id":"<id>","proposed_start":"HH:MM","reason":"..."} or
 {"type":"split","schedule_id":"<id>","parts":[{"start":"HH:MM","minutes":N},...],"reason":"..."}.
Use an empty items list when no change is requested or needed.

SCHEDULES:
{SCHEDULES}

USER QUESTION: {MESSAGE}
"""


def augment_chat_reply(reply: str, requested: int, validated: int) -> str:
    """Deterministic confirmation footer for a chat reply (v1.58.3).

    The model is asked to confirm proposals in its own words, but the guarantee
    lives here: state exactly what landed in the Apply card — including the
    awkward truth when validation rejected everything it drafted — so the chat
    never implies a change that didn't happen, or stays silent about one that did.
    """
    base = (reply or "").strip()
    if validated > 0:
        note = (
            f"✅ {validated} proposed change{'s' if validated != 1 else ''} ready "
            "in the card above — review and tap Apply to make it real."
        )
    elif requested > 0:
        note = (
            "⚠️ The changes I drafted didn't pass the safety rules (never drop a "
            "run, never change total minutes, split-chunk floors), so nothing was "
            "proposed. Try asking a different way."
        )
    else:
        return base
    return f"{base}\n\n{note}" if base else note


async def schedule_chat(hass, coord, message: str) -> dict[str, Any]:
    """Propose-only chat with the scheduling LLM. Returns {"reply", "proposed"}: a
    conversational answer plus a count of any change proposals, which are validated
    (never-drop) and stored as schedule_advice for the user to apply. Defensive:
    returns a friendly reply on any failure rather than raising."""
    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from homeassistant.util import dt as dt_util

        from .llm_client import call_chat, resolve_targets
        from .schedule_advisor import validate_schedule_advice

        msg = str(message or "").strip()[:1000]
        if not msg:
            return {"reply": "Ask me anything about your schedules.", "proposed": 0}
        targets = resolve_targets(coord.config)
        if not targets:
            return {
                "reply": "Attach an AI model first (Settings → Plant identification) "
                "and I can answer questions about your schedules.",
                "proposed": 0,
            }
        sched_text, by_id = _schedule_context(coord)
        prompt = _CHAT_PROMPT.replace("{SCHEDULES}", sched_text).replace("{MESSAGE}", msg)
        session = async_get_clientsession(hass)
        result = await call_chat(
            session,
            targets,
            [{"role": "user", "content": prompt}],
            timeout_s=120,
            max_tokens=4000,
            # Chat: accept ANY non-empty answer (a conversational reply needn't be
            # JSON) so a valid prose answer isn't rejected as "unreachable".
            accept=lambda c: bool(c and c.strip()),
        )
        if not result.ok:
            return {"reply": "I couldn't reach the model just now — try again.", "proposed": 0}
        content = result.content or ""
        obj = _extract_json_obj(content)
        if isinstance(obj, dict) and obj.get("reply"):
            # Structured envelope: use its reply + validate any proposals.
            reply = str(obj.get("reply") or "").strip()[:2000] or "(no reply)"
            advice = validate_schedule_advice(obj, schedules_by_id=by_id)
        else:
            # Prose-only answer: show it verbatim, no proposals.
            reply = content.strip()[:2000] or "(no reply)"
            advice = None
        proposed = 0
        if advice and advice.get("items"):
            advice["model"] = result.model or "llm"
            advice["created_at"] = dt_util.utcnow().isoformat()
            coord.config["schedule_advice"] = advice
            await coord.async_save_config()
            proposed = len(advice["items"])
        # v1.58.3 — deterministic confirmation: the reply always states what
        # actually landed in the Apply card (or that validation rejected it).
        requested = len(obj.get("items") or []) if isinstance(obj, dict) else 0
        reply = augment_chat_reply(reply, requested, proposed)
        return {"reply": reply, "proposed": proposed}
    except Exception:
        _LOGGER.exception("schedule chat failed")
        return {"reply": "Something went wrong handling that — try again.", "proposed": 0}


def _schedule_context(coord) -> tuple[str, dict[str, dict[str, Any]]]:
    """Compact per-schedule lines for the LLM + the id->meta map the validator needs."""
    lines: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    defaults = merge_chunk_defaults(coord.config.get("split_chunk_defaults"))
    for s in coord.schedule_store.enabled_schedules():
        chunk = effective_min_chunk(s.min_chunk_minutes, s.split_profile, defaults)
        by_id[s.id] = {
            "duration_minutes": s.duration_minutes,
            "min_chunk": chunk,
            "name": s.name,
        }
        cadence = "days " + ",".join(str(d) for d in s.weekdays) if s.mode == "weekdays" else s.mode
        lines.append(
            f"- id={s.id} name={s.name!r} zone={s.zone_entity_id} "
            f"start={s.start_time.strftime('%H:%M')} dur={s.duration_minutes}min "
            f"{'ESSENTIAL' if s.essential else 'non-essential'} "
            f"min_chunk={chunk} cadence=({cadence})"
        )
    return ("\n".join(lines) or "(none)"), by_id


def _detect(coord) -> list[dict[str, Any]]:
    """Plan a representative week and return all controller conflicts."""
    from homeassistant.util import dt as dt_util

    from .run_planner import next_runs
    from .schedule import DEFAULT_ZONE_BUFFER_SECONDS
    from .schedule_conflicts import detect_conflicts

    now = dt_util.now()
    zb = coord.config.get("zone_buffer_seconds")
    zb = int(zb) if zb is not None else DEFAULT_ZONE_BUFFER_SECONDS
    runs = next_runs(
        coord.schedule_store.enabled_schedules(),
        now,
        now + timedelta(days=7),
        zone_buffer_seconds=zb,
        sun_times=coord.sun_times,
    )
    return detect_conflicts(
        runs,
        # FLOOR (not round) so the minute-granularity detection buffer can never
        # exceed the planner's real second-granularity gap and flag a false overlap.
        buffer_minutes=max(0, int(zb) // 60),
        independent_zones=tuple(coord.independent_zones),
    )


def _unique_lines(conflicts: list[dict[str, Any]]) -> list[str]:
    from .schedule_conflicts import summarize_conflict

    lines: list[str] = []
    seen: set = set()
    for c in conflicts:
        key = tuple(sorted((c["a"].schedule_id, c["b"].schedule_id)))
        if key in seen:
            continue
        seen.add(key)
        lines.append(summarize_conflict(c))
    return lines


async def review_schedule(hass, coord, *, new_schedule_id: str | None = None) -> None:
    """Detect schedule conflicts and respond (propose via LLM, or notify for manual).

    ``new_schedule_id`` set → an add/edit check (report only conflicts involving it).
    ``None`` → the nightly safety sweep (report every conflict). Advisory + defensive:
    never raises, never actuates, never blocks a write.
    """
    try:
        from .llm_client import resolve_targets
        from .notifications import CATEGORY_IMPORTANT
        from .schedule_conflicts import conflicts_for_schedule

        conflicts = _detect(coord)
        if new_schedule_id is not None:
            conflicts = conflicts_for_schedule(conflicts, new_schedule_id)
        if not conflicts:
            return
        lines = _unique_lines(conflicts)

        if resolve_targets(coord.config):
            await _propose(hass, coord, lines)
            return
        if coord.notifier is not None:
            body = (
                "A schedule conflict was found:\n- "
                + "\n- ".join(lines[:5])
                + "\n\nAdjust the times manually, or set up a scheduling model to get "
                "proposed fixes. Nothing is missed — runs still fire (late if needed)."
            )
            await coord.notifier.notify(
                body,
                title="Schedule conflict",
                category=CATEGORY_IMPORTANT,
                event_type="schedule_conflict",
            )
    except Exception:
        _LOGGER.exception("schedule review failed (advisory; nothing actuated)")


async def _propose(hass, coord, conflict_lines: list[str]) -> None:
    """Ask the LLM for propose-only fixes, bound the reply, store + notify."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.util import dt as dt_util

    from .llm_client import call_chat, resolve_targets
    from .notifications import CATEGORY_IMPORTANT
    from .schedule_advisor import validate_schedule_advice

    targets = resolve_targets(coord.config)
    sched_text, by_id = _schedule_context(coord)
    if not targets or not by_id:
        return
    prompt = _PROMPT.replace("{SCHEDULES}", sched_text).replace(
        "{CONFLICTS}", "\n".join(f"- {c}" for c in conflict_lines) or "(none)"
    )
    session = async_get_clientsession(hass)
    result = await call_chat(
        session,
        targets,
        [{"role": "user", "content": prompt}],
        timeout_s=120,
        max_tokens=4000,
        accept=lambda c: _extract_json_obj(c) is not None,
    )
    if not result.ok:
        _LOGGER.info("schedule LLM review: no usable reply (%s)", result.error)
        return
    advice = validate_schedule_advice(_extract_json_obj(result.content), schedules_by_id=by_id)
    if advice is None or not advice.get("items"):
        return
    advice["model"] = result.model or "llm"
    advice["created_at"] = dt_util.utcnow().isoformat()
    coord.config["schedule_advice"] = advice
    await coord.async_save_config()
    if coord.notifier is not None:
        await coord.notifier.notify(
            f"The scheduler proposed {len(advice['items'])} adjustment(s) to resolve a "
            "schedule conflict. Review and apply them in the Schedules tab.",
            title="Schedule fix proposed",
            category=CATEGORY_IMPORTANT,
            event_type="schedule_advice_ready",
        )


def _extract_json_obj(content: str) -> dict[str, Any] | None:
    """Pull the first {...} JSON object out of an LLM reply (tolerant of prose/fences)."""
    if not content:
        return None
    import json as _json

    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = _json.loads(content[start : end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
