"""Notification dispatch — pure-logic queueing + HA-coupled delivery.

Every event the integration wants to surface goes through `notify`,
which:
  1. Always fires an HA event `complete_irrigation.notification` (so
     advanced users can wire automations for custom routing)
  2. If a notify target is configured AND the event isn't muted by
     quiet hours, calls `notify.<target>` to push to the user's phone
  3. If quiet hours suppress this category, queues for the morning summary

Categories:
  • critical    — pushed regardless of quiet hours
  • important   — quiet hours queue for summary; otherwise push immediately
  • informational — log only by default
"""

from __future__ import annotations

import logging
from datetime import time
from typing import TYPE_CHECKING, Any

from .const import DOMAIN

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

EVENT_NOTIFICATION = f"{DOMAIN}.notification"

CATEGORY_CRITICAL = "critical"
CATEGORY_IMPORTANT = "important"
CATEGORY_INFORMATIONAL = "informational"


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, start_str: str, end_str: str) -> bool:
    """Returns True if `now`'s time-of-day is within the quiet window.

    Handles wrap-around midnight (e.g., 22:00-07:00)."""
    start = _parse_hhmm(start_str)
    end = _parse_hhmm(end_str)
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _coerce_target_list(value: Any) -> list[str]:
    """Accept a list, comma- or newline-separated string, or None and
    return a clean list of non-empty notify.* service paths.

    v1.15: entries that are NOT in the `notify.*` domain are dropped with
    a warning. Without this guard, set_notification_config could install
    arbitrary `domain.service` strings (e.g. shell_command.curl_exfil)
    that would then be invoked on every notification event."""
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [p.strip() for p in value.replace("\n", ",").split(",")]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or s in seen:
            continue
        parts = s.split(".", 1)
        if len(parts) != 2 or parts[0] != "notify" or not parts[1]:
            _LOGGER.warning(
                "Dropping invalid notify target %r — must be of the form 'notify.<service>'",
                s,
            )
            continue
        seen.add(s)
        out.append(s)
    return out


class NotificationDispatcher:
    """Per-coordinator dispatcher. Owns the morning-summary queue."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._queue: list[dict[str, Any]] = []
        # Config (set via service):
        # Legacy single target (still accepted; mirrored into _targets).
        self.notify_target: str | None = None
        # Multiple notify services to fan out to. Each entry looks like
        # "notify.mobile_app_pete". Empty → fall back to notify_target.
        self.notify_targets: list[str] = []
        self.quiet_hours_start: str = "22:00"
        self.quiet_hours_end: str = "07:00"
        self.enabled: bool = True
        # Per-category mute (default: critical always on, others on)
        self.muted_categories: set[str] = set()
        # Not used by the dispatcher itself — coordinator / services
        # read these from the persisted config blob. Held here only so
        # update_config's allowlist accepts them without emitting a
        # spurious warning when the whole notifications blob is splatted.
        self.low_moisture_alerts: bool = True
        self.notify_on_missed: bool = True  # v1.17
        self.notify_on_aborted: bool = True  # v1.17.8

    # Explicit allowlist of attrs settable via update_config. setattr-based
    # fallback was too permissive — a typo like `notify_targest` would
    # silently stick on the dispatcher and the user would never know
    # their config didn't apply.
    _CONFIG_ALLOWLIST = frozenset(
        {
            "notify_target",
            "notify_targets",
            "quiet_hours_start",
            "quiet_hours_end",
            "enabled",
            "muted_categories",
            "low_moisture_alerts",
            "notify_on_missed",  # v1.17 — accepted, read by coordinator not dispatcher
            "notify_on_aborted",  # v1.17.8 — same shape, read in services.py
        }
    )

    def update_config(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if k == "notify_targets":
                self.notify_targets = _coerce_target_list(v)
            elif k == "notify_target":
                # Run the legacy single-target path through the same domain
                # validator so a bad value can't sneak in via setattr.
                cleaned = _coerce_target_list([v] if v else [])
                self.notify_target = cleaned[0] if cleaned else None
            elif k in self._CONFIG_ALLOWLIST:
                setattr(self, k, v)
            else:
                _LOGGER.warning(
                    "Ignoring unknown notification config key %r (allowed: %s)",
                    k,
                    sorted(self._CONFIG_ALLOWLIST),
                )

    def _resolved_targets(self) -> list[str]:
        """Return the list of notify.* services to fan out to.

        Prefers `notify_targets` when non-empty; otherwise falls back to
        the single `notify_target` for backward compat.
        """
        if self.notify_targets:
            return list(self.notify_targets)
        if self.notify_target:
            return [self.notify_target]
        return []

    async def notify(
        self,
        message: str,
        *,
        title: str | None = None,
        category: str = CATEGORY_IMPORTANT,
        event_type: str | None = None,
        extra: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Dispatch a notification through the configured rules.

        v1.17 — `actions` lets a caller attach inline action buttons
        (e.g. "Run now") for mobile_app notify targets. Non-mobile
        targets ignore actions and just get the message text. Format
        per HA Companion docs:
          [{"action": "STR_ID", "title": "Run now", "data": {...}}, ...]
        The Companion app fires `mobile_app_notification_action` events
        with `action` + `action_data` when buttons are tapped.
        """
        from homeassistant.util import dt as dt_util

        payload = {
            "category": category,
            "type": event_type or "generic",
            "title": title or "Complete Irrigation",
            "message": message,
            "extra": extra or {},
            "at": dt_util.now().isoformat(),
            "actions": list(actions) if actions else [],
        }

        # Always fire the HA event for automation routing.
        self._hass.bus.async_fire(EVENT_NOTIFICATION, payload)

        if not self.enabled or category in self.muted_categories:
            return

        # Quiet hours behavior:
        # - Critical → push immediately, ignore quiet hours
        # - Important → queue for morning summary during quiet hours
        # - Informational → never push, only summary at end of quiet hours
        in_quiet = in_quiet_hours(dt_util.now(), self.quiet_hours_start, self.quiet_hours_end)

        if category == CATEGORY_CRITICAL:
            await self._push_now(payload)
        elif category == CATEGORY_IMPORTANT:
            if in_quiet:
                self._queue.append(payload)
            else:
                await self._push_now(payload)
        elif category == CATEGORY_INFORMATIONAL:
            self._queue.append(payload)

    async def _push_now(self, payload: dict[str, Any]) -> None:
        # Local import — module-load coupling: pure-logic tests for
        # this file would otherwise need HA installed to even read the
        # source. Function-scope import preserves test isolation.
        from homeassistant.exceptions import ServiceNotFound

        targets = self._resolved_targets()
        if not targets:
            return
        actions = payload.get("actions") or []
        for target in targets:
            # Each target like "notify.mobile_app_pete" → domain "notify",
            # service "mobile_app_pete". One bad entry doesn't stop the rest.
            try:
                domain, service = target.split(".", 1)
            except ValueError:
                _LOGGER.warning("notify target must be 'notify.<service>', got %r", target)
                continue
            # Inline action buttons are HA-Companion-specific. Other
            # notify targets get the plain title/message and ignore the
            # extra data field (or treat it as additional metadata,
            # depending on integration). We attach data.actions only
            # when actions are present AND the target looks like a
            # mobile_app one to avoid sending non-spec payloads to e.g.
            # Telegram or Discord.
            service_data: dict[str, Any] = {
                "title": payload["title"],
                "message": payload["message"],
            }
            if actions and service.startswith("mobile_app"):
                service_data["data"] = {"actions": actions}
            try:
                await self._hass.services.async_call(
                    domain,
                    service,
                    service_data,
                    blocking=False,
                )
            except ServiceNotFound:
                # v1.17.14 — distinguish user-config drift (target was
                # configured but the underlying integration is no
                # longer installed / loaded) from a genuine notification
                # failure. The former is a configuration issue the
                # user can fix; warn-level keeps HA's logs clean.
                # Genuine delivery failures (network, integration
                # error) still log at ERROR via the broad except below.
                _LOGGER.warning(
                    "Notify target %s is not a registered service. Remove it "
                    "from Settings → Notifications, or install / restore "
                    "the integration that provides it.",
                    target,
                )
            except Exception:
                _LOGGER.exception("Failed to push notification via %s", target)

    async def flush_morning_summary(self) -> None:
        """Combine queued items into one notification and send."""
        if not self._queue:
            return
        if not self._resolved_targets():
            self._queue.clear()
            return

        lines = []
        for item in self._queue:
            lines.append(f"- {item['message']}")
        summary = "\n".join(lines)

        await self._push_now(
            {
                "title": "Irrigation — overnight summary",
                "message": f"{len(self._queue)} event(s) overnight:\n{summary}",
            }
        )
        self._queue.clear()
