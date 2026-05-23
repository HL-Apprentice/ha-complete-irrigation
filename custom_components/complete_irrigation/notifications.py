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

    def update_config(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if k == "notify_targets":
                # Normalize: accept list, comma-separated string, or None
                self.notify_targets = _coerce_target_list(v)
            elif k == "notify_target":
                # Run the legacy single-target path through the same domain
                # validator so a bad value can't sneak in via setattr.
                cleaned = _coerce_target_list([v] if v else [])
                self.notify_target = cleaned[0] if cleaned else None
            elif hasattr(self, k):
                setattr(self, k, v)

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
    ) -> None:
        """Dispatch a notification through the configured rules."""
        from homeassistant.util import dt as dt_util

        payload = {
            "category": category,
            "type": event_type or "generic",
            "title": title or "Complete Irrigation",
            "message": message,
            "extra": extra or {},
            "at": dt_util.now().isoformat(),
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
        targets = self._resolved_targets()
        if not targets:
            return
        for target in targets:
            # Each target like "notify.mobile_app_pete" → domain "notify",
            # service "mobile_app_pete". One bad entry doesn't stop the rest.
            try:
                domain, service = target.split(".", 1)
            except ValueError:
                _LOGGER.warning("notify target must be 'notify.<service>', got %r", target)
                continue
            try:
                await self._hass.services.async_call(
                    domain,
                    service,
                    {"title": payload["title"], "message": payload["message"]},
                    blocking=False,
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
