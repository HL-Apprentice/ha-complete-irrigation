"""Per-zone-type rain lockout - pure policy, no Home Assistant imports.

Event rainfall R is a 72-hour total. Tempest history and Open-Meteo
(``past_days=3``) are blended, then turned into a lockout length per zone
type. Daily ETo scales that length; the existing heat ceiling still caps it.

Zone types are read off data the integration already stores. Nothing here
writes a schedule or flips ``enabled``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .category_match import suggest_category
from .weather_gate import RAIN_LOCKOUT_FALLBACK_DAILY_ETO_IN

# Holding types get a multi-day lockout from the rain table.
ZONE_DEEP_DRIP = "deep_drip"
ZONE_FLOWERS_GARDEN = "flowers_garden"
ZONE_TURF = "turf"
# Birdbath: no residual lockout. It skips only while rain is actually falling.
ZONE_BIRDBATH = "birdbath"
# Germination / new-planting establishment: never locked out.
ZONE_GERMINATION = "germination"

HOLDING_ZONE_TYPES = (ZONE_DEEP_DRIP, ZONE_FLOWERS_GARDEN, ZONE_TURF)
ALL_ZONE_TYPES = (*HOLDING_ZONE_TYPES, ZONE_BIRDBATH, ZONE_GERMINATION)

# Agreement band: the two 72h totals "agree" when the gap is within
# max(0.15 in, 40% of the larger reading). Otherwise use the lower value
# plus 25% of the gap and raise RAIN_SOURCE_DISAGREE.
RAIN_AGREE_ABS_IN = 0.15
RAIN_AGREE_REL = 0.40
RAIN_DISAGREE_BLEND = 0.25
FLAG_RAIN_SOURCE_DISAGREE = "RAIN_SOURCE_DISAGREE"

# scale = clamp(0.12 / daily_ETo, 0.5, 1.5). 0.12 in/day is the 1.0x reference
# (a mild day). Hotter demand shortens the lockout; cool weather lengthens it,
# and the clamp keeps either extreme from running away.
ETO_SCALE_REF_IN = 0.12
ETO_SCALE_MIN = 0.5
ETO_SCALE_MAX = 1.5

FORECAST_EXTEND_MIN_IN = 0.25
FORECAST_EXTEND_MIN_PROB = 60.0
FORECAST_EXTEND_DAYS = 1

# >2 in: flowers+garden table value is 4 days, but a vegetable-garden zone
# sharing that clock is capped at 2 so greens are not held for a week.
GARDEN_HEAVY_RAIN_CAP_DAYS = 2
HEAVY_RAIN_IN = 2.0

# Table starts here even if the user sets the config floor to 0.
TABLE_MIN_INCHES = 0.10

# Birdbath hold while rain is falling. Refreshed by the coordinator; cleared
# as soon as rain stops. Not a multi-day lockout.
ACTIVE_RAIN_HOLD = timedelta(minutes=20)
ACTIVE_RAIN_REFRESH_WITHIN = timedelta(minutes=10)

# Hourly precip above this counts as "raining now" (Open-Meteo current hour).
ACTIVE_RAIN_HOUR_MM = 0.1

RAINY_WEATHER_STATES = frozenset(
    {
        "rainy",
        "pouring",
        "lightning",
        "lightning-rainy",
        "hail",
        "snowy-rainy",
    }
)

_DEEP_CATEGORIES = frozenset({"trees", "citrus", "bushes"})
_TURF_CATEGORIES = frozenset({"lawn"})
_GARDEN_CATEGORIES = frozenset({"vegetable_garden"})
_DEEP_PRESETS = frozenset({"tree", "shrub", "cactus_succulent"})
_TURF_PRESETS = frozenset({"grass"})
_FLOWER_PRESETS = frozenset({"flower"})

# (deep drip, flowers+garden, turf) days. Buckets are [lo, hi) except the
# 1-2 in bucket, which includes exactly 2.0. Above 2.0 is the last row.
_BUCKETS: tuple[tuple[float, float, tuple[int, int, int]], ...] = (
    (0.10, 0.25, (1, 1, 1)),
    (0.25, 0.50, (2, 1, 2)),
    (0.50, 1.00, (4, 2, 3)),
    (1.00, 2.00, (6, 3, 4)),  # inclusive of 2.0 - see table_days()
)


def _clean_inches(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


@dataclass(frozen=True)
class RainBlend:
    """72h rainfall after the Tempest / Open-Meteo blend."""

    inches: float | None
    tempest_in: float | None
    open_meteo_in: float | None
    disagree: bool
    used: str  # mean | conservative | tempest_only | open_meteo_only | none

    @property
    def flag(self) -> str | None:
        return FLAG_RAIN_SOURCE_DISAGREE if self.disagree else None


def combine_rain_sources(tempest_in: Any, open_meteo_in: Any) -> RainBlend:
    """Blend two 72h totals.

    One missing source falls back to the other (``disagree`` stays false -
    there is nothing to disagree with). Both missing -> no rainfall. When both
    exist and the gap exceeds ``max(0.15 in, 40% of the larger)``, the result
    is ``lower + 25% of the gap`` and ``disagree`` is set.
    """
    tempest = _clean_inches(tempest_in)
    meteo = _clean_inches(open_meteo_in)
    if tempest is None and meteo is None:
        return RainBlend(None, None, None, False, "none")
    if tempest is None:
        return RainBlend(meteo, None, meteo, False, "open_meteo_only")
    if meteo is None:
        return RainBlend(tempest, tempest, None, False, "tempest_only")
    lo, hi = (tempest, meteo) if tempest <= meteo else (meteo, tempest)
    gap = hi - lo
    tolerance = max(RAIN_AGREE_ABS_IN, RAIN_AGREE_REL * hi)
    if gap <= tolerance:
        return RainBlend((tempest + meteo) / 2.0, tempest, meteo, False, "mean")
    return RainBlend(lo + RAIN_DISAGREE_BLEND * gap, tempest, meteo, True, "conservative")


def table_days(rainfall_in: float) -> tuple[int, int, int] | None:
    """(deep drip, flowers+garden, turf) days for R, or None below 0.10 in.

    Exactly 2.0 in stays in the 1-2 in row. Anything above 2.0 in is the last row.
    """
    if not math.isfinite(rainfall_in) or rainfall_in < TABLE_MIN_INCHES:
        return None
    if rainfall_in <= HEAVY_RAIN_IN:
        for lo, hi, days in _BUCKETS:
            # The 1-2 bucket's upper bound is inclusive; the others are [lo, hi).
            if lo == 1.00:
                if lo <= rainfall_in <= hi:
                    return days
            elif lo <= rainfall_in < hi:
                return days
        return None
    return (7, 4, 5)


def base_lockout_days(
    rainfall_in: float,
    zone_type: str,
    *,
    garden_present: bool = False,
    min_inches: float = TABLE_MIN_INCHES,
) -> int | None:
    """Days from the rain table for one zone type.

    Birdbath and germination are always None (0 days). A configured floor
    above the table's 0.10 in start suppresses lighter events. When a
    vegetable-garden zone shares the flowers+garden clock and R > 2 in, that
    clock is capped at 2 days.
    """
    if zone_type not in HOLDING_ZONE_TYPES:
        return None
    try:
        rain = float(rainfall_in)
        floor = float(min_inches)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rain):
        return None
    if math.isfinite(floor) and floor > 0 and rain < floor:
        return None
    row = table_days(rain)
    if row is None:
        return None
    index = HOLDING_ZONE_TYPES.index(zone_type)
    days = row[index]
    if (
        zone_type == ZONE_FLOWERS_GARDEN
        and garden_present
        and rain > HEAVY_RAIN_IN
        and days > GARDEN_HEAVY_RAIN_CAP_DAYS
    ):
        return GARDEN_HEAVY_RAIN_CAP_DAYS
    return days


def reference_daily_eto(*values: Any) -> float | None:
    """Hottest demand wins: the max of the current and forecast daily ETo values.

    A larger ETo shortens the lockout (the scale is ``0.12 / ETo``). Missing
    or non-positive inputs are ignored. None when nothing usable was passed -
    the scaler then uses the short summer fallback.
    """
    usable: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            usable.append(number)
    return max(usable) if usable else None


def eto_scale_factor(daily_eto_in: float | None) -> float:
    """``clamp(0.12 / daily_ETo, 0.5, 1.5)``. Missing ETo uses the 0.25 in fallback."""
    eto = daily_eto_in
    if eto is None or not math.isfinite(eto) or eto <= 0:
        eto = RAIN_LOCKOUT_FALLBACK_DAILY_ETO_IN
    return min(ETO_SCALE_MAX, max(ETO_SCALE_MIN, ETO_SCALE_REF_IN / eto))


def scaled_lockout_hours(
    days: float,
    daily_eto_in: float | None,
    max_hours: float | None,
) -> float:
    """Table days x ETo scale x 24, then the heat ceiling (``max_hours``)."""
    hours = float(days) * eto_scale_factor(daily_eto_in) * 24.0
    if max_hours is not None and math.isfinite(max_hours) and max_hours > 0:
        hours = min(hours, float(max_hours))
    return hours


def forecast_extends_lockout(precip_in: Any, probability_pct: Any) -> bool:
    """True when the next 24h calls for ≥0.25 in at ≥60% probability."""
    try:
        inches = float(precip_in)
        probability = float(probability_pct)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(inches) or not math.isfinite(probability):
        return False
    return inches >= FORECAST_EXTEND_MIN_IN and probability >= FORECAST_EXTEND_MIN_PROB


def extended_until(
    current: datetime | None,
    now: datetime,
    *,
    extra_days: float = FORECAST_EXTEND_DAYS,
    max_hours: float | None = None,
) -> datetime | None:
    """Push an active ``current`` forward by ``extra_days``, clamped to the heat ceiling.

    Does not start a lockout. If the ceiling already holds ``current``, the
    same timestamp comes back (no growth).
    """
    if current is None or not (now < current):
        return current
    proposed = current + timedelta(days=float(extra_days))
    if max_hours is not None and math.isfinite(max_hours) and max_hours > 0:
        ceiling = now + timedelta(hours=float(max_hours))
        if proposed > ceiling:
            proposed = ceiling
    if proposed <= current:
        return current
    return proposed


def moisture_releases_early(current_pct: Any, min_pct: Any) -> bool:
    """A reading strictly below the zone minimum may end a lockout early.

    Anything else - including a saturated reading - is not a release, and this
    function never produces a longer lockout. Non-numeric / non-finite values
    are not a release.
    """
    try:
        current = float(current_pct)
        minimum = float(min_pct)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(current) or not math.isfinite(minimum):
        return False
    return current < minimum


def weather_state_is_rain(state: Any) -> bool:
    return str(state or "").strip().lower() in RAINY_WEATHER_STATES


def detect_active_rain(
    *,
    weather_states: list[Any] | tuple[Any, ...] = (),
    today_in: float | None = None,
    previous_today_in: float | None = None,
    current_precip_mm: float | None = None,
) -> bool:
    """True only while rain is falling, not because a daily total is merely elevated.

    A rising edge on today's accumulation counts (the total grew since the last
    read). The first sample has no previous value and does not count - a sensor
    that already reads 0.4 in at startup is not proof that it is raining now.
    """
    if any(weather_state_is_rain(state) for state in weather_states):
        return True
    if (
        today_in is not None
        and previous_today_in is not None
        and today_in > previous_today_in + 0.001
    ):
        return True
    return current_precip_mm is not None and current_precip_mm > ACTIVE_RAIN_HOUR_MM


def _majority(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    # Stable: first-seen wins a tie (values are already in a caller-chosen order).
    return max(counts, key=lambda key: (counts[key], -values.index(key)))


def _preset_zone_type(preset: str | None) -> str | None:
    if preset in _TURF_PRESETS:
        return ZONE_TURF
    if preset in _FLOWER_PRESETS:
        return ZONE_FLOWERS_GARDEN
    if preset in _DEEP_PRESETS:
        return ZONE_DEEP_DRIP
    return None


def zone_is_garden(*, category: str | None, entity_id: str = "", friendly_name: str = "") -> bool:
    """Vegetable garden (the >2 in cap), as opposed to a flower bed."""
    if category in _GARDEN_CATEGORIES:
        return True
    blob = f"{entity_id} {friendly_name}".lower()
    return "garden" in blob or "veg" in blob


def classify_zone(
    *,
    entity_id: str,
    friendly_name: str = "",
    category: str | None = None,
    independent: bool = False,
    schedule_profiles: tuple[str, ...] | list[str] = (),
    plant_presets: tuple[str, ...] | list[str] = (),
    establishment_active: bool = False,
) -> str:
    """Map one zone onto a lockout type using config that already exists.

    Order, most specific first:

    1. Germination - an in-window establishment schedule, or a name that says
       germination / new grass. These never take a rain lockout.
    2. Birdbath - the name says birdbath, or the zone is on its own supply
       (``independent_zones``, whose documented use is a birdbath fill) and
       nothing else classified it.
    3. The zone's stored plant ``category`` (lawn / vegetable_garden / trees /
       citrus / bushes).
    4. Majority ``care_plan_preset`` of plants on the zone, then majority
       schedule ``split_profile``.
    5. Name keywords (the same ones setup uses, plus "flower").
    6. Otherwise turf. Unconfigured zones already default to the lawn moisture
       band, so the lockout default matches that.
    """
    blob = f"{entity_id} {friendly_name}".lower()
    if establishment_active or "germinat" in blob or "new grass" in blob or "new_grass" in blob:
        return ZONE_GERMINATION
    if any(token in blob for token in ("birdbath", "bird bath", "bird_bath", "bird-bath")):
        return ZONE_BIRDBATH
    if category in _TURF_CATEGORIES:
        return ZONE_TURF
    if category in _GARDEN_CATEGORIES:
        return ZONE_FLOWERS_GARDEN
    if category in _DEEP_CATEGORIES:
        return ZONE_DEEP_DRIP
    preset_type = _preset_zone_type(_majority([p for p in plant_presets if p]))
    if preset_type:
        return preset_type
    profile_type = _preset_zone_type(_majority([p for p in schedule_profiles if p]))
    if profile_type:
        return profile_type
    suggested = suggest_category(blob)
    if suggested == "lawn":
        return ZONE_TURF
    if suggested == "vegetable_garden":
        return ZONE_FLOWERS_GARDEN
    if suggested in _DEEP_CATEGORIES:
        return ZONE_DEEP_DRIP
    if "flower" in blob:
        return ZONE_FLOWERS_GARDEN
    if independent:
        return ZONE_BIRDBATH
    return ZONE_TURF


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class StoredRainLockout:
    """The lockout document persisted in its own Store key."""

    until_by_type: dict[str, datetime | None] = field(default_factory=dict)
    last_event_rain_in: float | None = None
    rain_source_disagree: bool = False
    forecast_extended: set[str] = field(default_factory=set)
    sources: dict[str, Any] = field(default_factory=dict)
    previous_today_in: float | None = None

    def __post_init__(self) -> None:
        until = {zone_type: self.until_by_type.get(zone_type) for zone_type in ALL_ZONE_TYPES}
        until[ZONE_GERMINATION] = None
        self.until_by_type = until
        self.forecast_extended = {z for z in self.forecast_extended if z in HOLDING_ZONE_TYPES}

    def to_dict(self) -> dict[str, Any]:
        return {
            "lockout_until": {
                zone_type: (
                    self.until_by_type.get(zone_type).isoformat()
                    if self.until_by_type.get(zone_type)
                    else None
                )
                for zone_type in ALL_ZONE_TYPES
            },
            "last_event_rain_in": self.last_event_rain_in,
            "rain_source_disagree": bool(self.rain_source_disagree),
            "forecast_extended": sorted(self.forecast_extended),
            "sources": dict(self.sources),
            "previous_today_in": self.previous_today_in,
        }

    @classmethod
    def from_dict(cls, data: Any) -> StoredRainLockout:
        if not isinstance(data, dict):
            data = {}
        raw_until = data.get("lockout_until")
        until: dict[str, datetime | None] = {}
        if isinstance(raw_until, dict):
            for zone_type in ALL_ZONE_TYPES:
                until[zone_type] = _parse_dt(raw_until.get(zone_type))
        last = _clean_inches(data.get("last_event_rain_in"))
        # 0.0 is a legitimate baseline; _clean_inches keeps it. None stays None.
        if data.get("last_event_rain_in") is None:
            last = None
        extended_raw = data.get("forecast_extended")
        extended = (
            {str(item) for item in extended_raw if str(item) in HOLDING_ZONE_TYPES}
            if isinstance(extended_raw, list)
            else set()
        )
        sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
        return cls(
            until_by_type=until,
            last_event_rain_in=last,
            rain_source_disagree=bool(data.get("rain_source_disagree")),
            forecast_extended=extended,
            sources=dict(sources),
            previous_today_in=_clean_inches(data.get("previous_today_in")),
        )


def _blank_state() -> StoredRainLockout:
    return StoredRainLockout()


def release_zone_type(state: StoredRainLockout, zone_type: str) -> StoredRainLockout:
    """A completed watering ends that type's lockout. It does not clear the rain baseline.

    Leaving ``last_event_rain_in`` in place is what stops the same 72h total from
    immediately re-arming the lockout on the next tick.
    """
    if zone_type not in ALL_ZONE_TYPES or zone_type == ZONE_GERMINATION:
        return state
    if state.until_by_type.get(zone_type) is None and zone_type not in state.forecast_extended:
        return state
    until = dict(state.until_by_type)
    until[zone_type] = None
    return StoredRainLockout(
        until_by_type=until,
        last_event_rain_in=state.last_event_rain_in,
        rain_source_disagree=state.rain_source_disagree,
        forecast_extended=set(state.forecast_extended) - {zone_type},
        sources=dict(state.sources),
        previous_today_in=state.previous_today_in,
    )


def _untils_equal(left: dict[str, datetime | None], right: dict[str, datetime | None]) -> bool:
    return all(left.get(zone_type) == right.get(zone_type) for zone_type in ALL_ZONE_TYPES)


@dataclass(frozen=True)
class LockoutUpdate:
    state: StoredRainLockout
    changed: bool
    logs: tuple[str, ...]


def decide_lockouts(
    prev: StoredRainLockout | None,
    *,
    now: datetime,
    rainfall_in: float | None,
    blend: RainBlend,
    daily_eto_in: float | None,
    max_hours: float | None,
    garden_present: bool,
    active_rain: bool,
    forecast_in: float | None,
    forecast_probability_pct: float | None,
    dry_zone_types: set[str] | frozenset[str] = frozenset(),
    min_inches: float = TABLE_MIN_INCHES,
    today_in: float | None = None,
) -> LockoutUpdate:
    """Next lockout state.

    Rainfall (rising 72h total) and a qualifying forecast may set or extend a
    holding type. Soil moisture may only clear one. Birdbath follows active
    rain. Germination stays clear. The same rainfall total does not push an
    existing ``until`` later - that was the stuck-timer bug.
    """
    state = prev if prev is not None else _blank_state()
    until = {zone_type: state.until_by_type.get(zone_type) for zone_type in ALL_ZONE_TYPES}
    extended = set(state.forecast_extended)
    last = state.last_event_rain_in
    logs: list[str] = []

    # ── birdbath: only while rain is falling ──────────────────────
    if active_rain:
        horizon = now + ACTIVE_RAIN_HOLD
        current = until.get(ZONE_BIRDBATH)
        if current is None or current < now + ACTIVE_RAIN_REFRESH_WITHIN:
            until[ZONE_BIRDBATH] = horizon
            logs.append("birdbath lockout while rain is falling")
    elif until.get(ZONE_BIRDBATH) is not None:
        until[ZONE_BIRDBATH] = None
        logs.append("birdbath lockout cleared - rain stopped")
    until[ZONE_GERMINATION] = None

    # ── event rain arms / extends holding types on a rising total ─
    rain = _clean_inches(rainfall_in) if rainfall_in is not None else None
    if rain is None and rainfall_in is not None:
        rain = None
    if rain is not None:
        rose = last is None or rain > last + 0.001
        dropped = last is not None and rain < last - 0.01
        if rose:
            last = rain
            for zone_type in HOLDING_ZONE_TYPES:
                days = base_lockout_days(
                    rain, zone_type, garden_present=garden_present, min_inches=min_inches
                )
                if days is None:
                    continue
                hours = scaled_lockout_hours(days, daily_eto_in, max_hours)
                proposed = now + timedelta(hours=hours)
                current = until.get(zone_type)
                if current is None or proposed > current:
                    until[zone_type] = proposed
                    extended.discard(zone_type)
                    logs.append(
                        f"{zone_type} lockout {hours:.1f}h from {rain:.2f} in "
                        f"({days}d x ETo scale, heat ceiling {max_hours}h)"
                    )
        elif dropped:
            last = rain
            logs.append(f"rain total fell to {rain:.2f} in - baseline reset, lockout not extended")

    # ── forecast may add one day to a lockout that is already active
    if forecast_extends_lockout(forecast_in, forecast_probability_pct):
        for zone_type in HOLDING_ZONE_TYPES:
            current = until.get(zone_type)
            if current is None or not (now < current) or zone_type in extended:
                continue
            proposed = extended_until(current, now, max_hours=max_hours)
            extended.add(zone_type)
            if proposed is not None and proposed > current:
                until[zone_type] = proposed
                logs.append(
                    f"{zone_type} lockout extended 1d for forecast "
                    f"{forecast_in:.2f} in @ {forecast_probability_pct:.0f}%"
                )
            else:
                logs.append(
                    f"{zone_type} forecast extension held at the heat ceiling ({max_hours}h)"
                )

    # ── moisture may only release, and only types the caller flagged
    for zone_type in dry_zone_types:
        if zone_type not in HOLDING_ZONE_TYPES:
            continue
        if until.get(zone_type) is not None:
            until[zone_type] = None
            extended.discard(zone_type)
            logs.append(f"{zone_type} lockout released - soil moisture below minimum")

    sources = {
        "tempest_in": blend.tempest_in,
        "open_meteo_in": blend.open_meteo_in,
        "combined_in": blend.inches,
        "used": blend.used,
        "forecast_24h_in": forecast_in,
        "forecast_24h_probability_pct": forecast_probability_pct,
        "flag": blend.flag,
    }
    updated = StoredRainLockout(
        until_by_type=until,
        last_event_rain_in=last,
        rain_source_disagree=blend.disagree,
        forecast_extended=extended,
        sources=sources,
        previous_today_in=today_in if today_in is not None else state.previous_today_in,
    )
    changed = (
        not _untils_equal(state.until_by_type, updated.until_by_type)
        or state.last_event_rain_in != updated.last_event_rain_in
        or state.rain_source_disagree != updated.rain_source_disagree
        or state.forecast_extended != updated.forecast_extended
    )
    return LockoutUpdate(state=updated, changed=changed, logs=tuple(logs))
